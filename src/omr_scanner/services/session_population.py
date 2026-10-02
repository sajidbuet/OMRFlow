"""The scan session's effective sheet set: one answer, for every stage (0.1.1 phase 4).

Purpose:
    **The canonical answer to "which recorded sheets currently count for this
    scan session?"** Attendance, scoring, Results, Reports, final export,
    duplicate-ID detection and the Resolve queue all ask here, so no two
    stages can disagree about the population. See
    ``docs/decisions/ADR-0007-session-effective-sheet-set.md``.

Batch versus session:
    A **batch** is an operational unit - one import, one run, its progress,
    resume and provenance. A **scan session** is the examination: its
    effective sheet set is a *projection* of every batch's persisted history,
    computed on demand from ``batch_scan``, ``scan_rejection``,
    ``batch_supersession`` and ``scan_batch.scan_session_id``. Nothing is
    deleted to make a sheet stop counting, nothing is stored to make it count,
    and no image is read.

Responsibilities:
    * :func:`population` - every sheet of a session with its
      :class:`~omr_scanner.domain.session_population.SheetDisposition`.
    * :func:`population_key` - the one ``batch_id`` value under which a
      session's downstream state (reconciliation, results) is stored.
    * :func:`effective_identifiers` / :func:`effective_set_codes` /
      :func:`effective_answers` / :func:`results_by_scan` - the review
      ledger's effective values and the stored results for a population's
      sheets, read from each sheet's own batch where its records live.

The population key (why no migration):
    Downstream tables are keyed ``(roster_id, batch_id)``. Rather than re-key
    them, a session's downstream state is stored under one deterministic batch
    of the session - :func:`population_key`: the **oldest batch of the session
    that already holds downstream state**, else the session's oldest batch.
    Once state exists under it the key never moves, so adding a batch, a
    rescan batch or a reprocess batch never orphans an operator's
    reconciliation decisions. For a one-batch session - every project upgraded
    from before scan sessions - the key is that batch, so stored state reads
    exactly as before. The key names the population; it is not a member of
    it (it may even be a superseded batch).

Sessions:
    A batch with no session (a pre-session database opened read-only, or one
    written before the backfill ran) is its own one-batch population.
    Session-wide never means project-wide: another session's sheets never
    enter this one's population, duplicate check or results - with one
    explicit exception, a confirmed rescan whose original belongs to this
    session (a link made before 0.1.1 phase 4 refused cross-session links),
    which counts in its original's session and is reported by Project Health.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from omr_scanner.database.models import (
    BatchScan,
    BatchSupersession,
    CandidateResult,
    ReconciliationRun,
    ScanBatch,
    ScanRejection,
)
from omr_scanner.domain.session_population import (
    SheetDisposition,
    SheetFacts,
    classify,
    lineage_roots,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import set_identity
    from omr_scanner.services.recognition_models import ScanResult
    from omr_scanner.services.review_store import EffectiveAnswers, EffectiveIdentifier

_LOGGER = logging.getLogger(__name__)

LONE_BATCH_PREFIX = "batch:"
"""Session id given to a batch that belongs to no scan session."""


@dataclass(frozen=True, slots=True)
class SessionPopulation:
    """Every sheet recorded in one scan session, and what it means now.

    Attributes:
        session_id: The scan session (``batch:<id>`` for a batch with none).
        key_batch_id: The population key (see the module docstring).
        batch_ids: Every batch of the session, oldest first.
        live_batch_ids: Those not replaced by a live batch supersession.
        dispositions: ``scan id -> disposition`` for every sheet of every
            batch of the session, plus confirmed rescans read into another
            session whose lineage counts here.
        batch_of: ``scan id -> the batch it was read into``, same keys.
        lineage_root: ``scan id -> lineage root`` for sheets in a rescan link.
    """

    session_id: str
    key_batch_id: str
    batch_ids: tuple[str, ...]
    live_batch_ids: tuple[str, ...]
    dispositions: dict[int, SheetDisposition] = field(default_factory=dict)
    batch_of: dict[int, str] = field(default_factory=dict)
    lineage_root: dict[int, int] = field(default_factory=dict)

    @property
    def effective(self) -> frozenset[int]:
        """The effective sheet set: every sheet that counts."""
        return frozenset(scan for scan, kind in self.dispositions.items() if kind.counts)

    @property
    def listed(self) -> frozenset[int]:
        """Sheets Attendance lists without counting (rescan awaited, deferred)."""
        return frozenset(scan for scan, kind in self.dispositions.items() if kind.is_listed)

    @property
    def reconciled(self) -> frozenset[int]:
        """What reconciliation is given: the effective set plus the listed sheets."""
        return self.effective | self.listed

    def with_disposition(self, *kinds: SheetDisposition) -> frozenset[int]:
        """Sheets in any of ``kinds``."""
        wanted = set(kinds)
        return frozenset(scan for scan, kind in self.dispositions.items() if kind in wanted)

    def counts(self) -> dict[SheetDisposition, int]:
        """How many sheets are in each disposition."""
        return dict(Counter(self.dispositions.values()))

    @property
    def contributing_batch_ids(self) -> tuple[str, ...]:
        """Batches holding at least one effective sheet, in session order."""
        holding = {self.batch_of[scan] for scan in self.effective}
        ordered = [item for item in self.batch_ids if item in holding]
        ordered += sorted(holding - set(ordered))
        return tuple(ordered)

    def batches_holding(self, scan_ids: Iterable[int]) -> tuple[str, ...]:
        """The batches the given sheets were read into (their records live there)."""
        found = {self.batch_of[scan] for scan in scan_ids if scan in self.batch_of}
        ordered = [item for item in self.batch_ids if item in found]
        ordered += sorted(found - set(ordered))
        return tuple(ordered)


# ----------------------------------------------------------------------
# Sessions and keys
# ----------------------------------------------------------------------
def _has_sessions(database: ProjectDatabase) -> bool:
    from omr_scanner.services import scan_sessions

    return scan_sessions.has_lifecycle_schema(database)


def _session_batches(session: Session, session_id: str) -> list[tuple[str, object]]:
    """``(batch_id, created_at)`` of a session's batches, oldest first."""
    if session_id.startswith(LONE_BATCH_PREFIX):
        batch_id = session_id.removeprefix(LONE_BATCH_PREFIX)
        found = session.execute(
            select(ScanBatch.batch_id, ScanBatch.created_at).where(ScanBatch.batch_id == batch_id)
        ).all()
    else:
        found = session.execute(
            select(ScanBatch.batch_id, ScanBatch.created_at)
            .where(ScanBatch.scan_session_id == session_id)
            .order_by(ScanBatch.created_at, ScanBatch.batch_id)
        ).all()
    return [(str(batch), created) for batch, created in found]


def session_of_batch(database: ProjectDatabase, batch_id: str) -> str:
    """The scan session a batch belongs to (``batch:<id>`` when it has none)."""
    if not _has_sessions(database):
        return f"{LONE_BATCH_PREFIX}{batch_id}"
    with database.session() as session:
        found = session.scalar(
            select(ScanBatch.scan_session_id).where(ScanBatch.batch_id == batch_id)
        )
    return str(found) if found else f"{LONE_BATCH_PREFIX}{batch_id}"


def _key_for(session: Session, batches: Sequence[str]) -> str:
    """The oldest of ``batches`` already holding downstream state, else the oldest."""
    if not batches:
        return ""
    holding = set(
        session.scalars(
            select(ReconciliationRun.batch_id).where(ReconciliationRun.batch_id.in_(batches))
        ).all()
    ) | set(
        session.scalars(
            select(CandidateResult.batch_id)
            .where(CandidateResult.batch_id.in_(batches))
            .distinct()
        ).all()
    )
    for batch_id in batches:
        if batch_id in holding:
            return batch_id
    return batches[0]


def population_key(database: ProjectDatabase, batch_id: str) -> str:
    """The ``batch_id`` value a session's downstream state is stored under.

    Args:
        database: The open project database.
        batch_id: Any batch of the session.

    Returns:
        The population key (see the module docstring). ``batch_id`` itself
        when it is unknown, so a caller never loses its argument.
    """
    session_id = session_of_batch(database, batch_id)
    with database.session() as session:
        batches = [item for item, _created in _session_batches(session, session_id)]
        key = _key_for(session, batches)
    return key or batch_id


def session_key(database: ProjectDatabase, scan_session_id: str) -> str | None:
    """The population key of a scan session, or ``None`` when it has no batch."""
    with database.session() as session:
        batches = [item for item, _created in _session_batches(session, scan_session_id)]
        return _key_for(session, batches) or None


# ----------------------------------------------------------------------
# The population
# ----------------------------------------------------------------------
def population(database: ProjectDatabase, batch_id: str) -> SessionPopulation:
    """The session population of the session ``batch_id`` belongs to."""
    return session_population(database, session_of_batch(database, batch_id))


def session_population(database: ProjectDatabase, scan_session_id: str) -> SessionPopulation:
    """Every sheet of one scan session with its disposition. Read-only.

    A bounded number of indexed queries, whatever the session's size: the
    session's batches, their scans, the lifecycle rows of those scans, and the
    project's rescan links (one row per confirmed rescan). Lineage is walked
    in Python, linearly.
    """
    with database.session() as session:
        batch_rows = _session_batches(session, scan_session_id)
        batches = [item for item, _created in batch_rows]
        key = _key_for(session, batches)
        superseded: set[str] = set()
        if _has_sessions(database):
            superseded = {
                str(item)
                for item in session.scalars(
                    select(BatchSupersession.superseded_batch_id).where(
                        BatchSupersession.reversed_at.is_(None)
                    )
                ).all()
            }

        scans = session.execute(
            select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.status).where(
                BatchScan.batch_id.in_(batches)
            )
        ).all()
        lifecycle = {
            int(scan_id): str(state)
            for scan_id, state in session.execute(
                select(ScanRejection.scan_id, ScanRejection.state)
                .join(BatchScan, BatchScan.scan_id == ScanRejection.scan_id)
                .where(BatchScan.batch_id.in_(batches))
            ).all()
        }

        # Every confirmed rescan link in the project: few rows (one per
        # rescan), and the only way a lineage can cross a session boundary.
        links = session.execute(
            select(ScanRejection.scan_id, ScanRejection.replacement_scan_id).where(
                ScanRejection.replacement_scan_id.is_not(None)
            )
        ).all()
        replacement_of = {int(new): int(old) for old, new in links if new is not None}
        lineage = lineage_roots(replacement_of)
        linked = set(lineage.root_of) | set(lineage.cycles)
        link_batches = {
            int(scan_id): str(batch)
            for scan_id, batch in session.execute(
                select(BatchScan.scan_id, BatchScan.batch_id).where(
                    BatchScan.scan_id.in_(sorted(linked))
                )
            ).all()
        } if linked else {}
        batch_sessions = _batch_sessions(session, database, set(link_batches.values()))

        def counting_session(scan_id: int) -> str | None:
            root = lineage.root_of.get(scan_id)
            if root is None:
                return None
            root_batch = link_batches.get(root)
            if root_batch is None:
                return None
            return batch_sessions.get(root_batch)

        dispositions: dict[int, SheetDisposition] = {}
        batch_of: dict[int, str] = {}
        for scan_id, scan_batch, status in scans:
            scan = int(scan_id)
            facts = SheetFacts(
                scan_id=scan,
                batch_id=str(scan_batch),
                status=str(status),
                lifecycle=lifecycle.get(scan, "active"),
                batch_superseded=str(scan_batch) in superseded,
                counting_session=counting_session(scan),
            )
            dispositions[scan] = classify(facts, scan_session_id)
            batch_of[scan] = str(scan_batch)

        # Confirmed rescans read into another session whose lineage is rooted
        # here: they count here (and are reported by Project Health).
        adopted = [
            scan
            for scan in lineage.root_of
            if scan not in dispositions
            and counting_session(scan) == scan_session_id
            and link_batches.get(scan) is not None
        ]
        if adopted:
            for scan_id, scan_batch, status in session.execute(
                select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.status).where(
                    BatchScan.scan_id.in_(adopted)
                )
            ).all():
                scan = int(scan_id)
                state = session.scalar(
                    select(ScanRejection.state).where(ScanRejection.scan_id == scan)
                )
                facts = SheetFacts(
                    scan_id=scan,
                    batch_id=str(scan_batch),
                    status=str(status),
                    lifecycle=str(state) if state else "active",
                    batch_superseded=str(scan_batch) in superseded,
                    counting_session=scan_session_id,
                )
                dispositions[scan] = classify(facts, scan_session_id)
                batch_of[scan] = str(scan_batch)

    return SessionPopulation(
        session_id=scan_session_id,
        key_batch_id=key,
        batch_ids=tuple(batches),
        live_batch_ids=tuple(item for item in batches if item not in superseded),
        dispositions=dispositions,
        batch_of=batch_of,
        lineage_root={
            scan: root for scan, root in lineage.root_of.items() if scan in dispositions
        },
    )


def _batch_sessions(
    session: Session, database: ProjectDatabase, batch_ids: set[str]
) -> dict[str, str]:
    """``batch -> session id`` (``batch:<id>`` for a batch with none)."""
    if not batch_ids:
        return {}
    if not _has_sessions(database):
        return {item: f"{LONE_BATCH_PREFIX}{item}" for item in batch_ids}
    found = {
        str(batch): (str(owner) if owner else f"{LONE_BATCH_PREFIX}{batch}")
        for batch, owner in session.execute(
            select(ScanBatch.batch_id, ScanBatch.scan_session_id).where(
                ScanBatch.batch_id.in_(sorted(batch_ids))
            )
        ).all()
    }
    return found


def effective_count(database: ProjectDatabase, batch_id: str) -> int:
    """How many sheets count in the session of ``batch_id``."""
    return len(population(database, batch_id).effective)


# ----------------------------------------------------------------------
# Values for a population's sheets, from each sheet's own batch
# ----------------------------------------------------------------------
def _per_batch(
    population_: SessionPopulation, scan_ids: Iterable[int] | None
) -> tuple[frozenset[int], tuple[str, ...]]:
    wanted = frozenset(scan_ids) if scan_ids is not None else frozenset(population_.dispositions)
    return wanted, population_.batches_holding(wanted)


def effective_identifiers(
    database: ProjectDatabase,
    population_: SessionPopulation,
    scan_ids: Iterable[int] | None = None,
) -> dict[int, EffectiveIdentifier]:
    """Post-review Student IDs of ``scan_ids`` (default: every sheet of the population)."""
    from omr_scanner.services import review_store

    wanted, batches = _per_batch(population_, scan_ids)
    found: dict[int, EffectiveIdentifier] = {}
    for batch_id in batches:
        for scan_id, item in review_store.effective_identifiers(database, batch_id).items():
            if scan_id in wanted:
                found[scan_id] = item
    return found


def effective_set_codes(
    database: ProjectDatabase,
    population_: SessionPopulation,
    scan_ids: Iterable[int] | None = None,
    *,
    identity: set_identity.SetIdentity | None = None,
) -> dict[int, EffectiveIdentifier]:
    """Post-review (logical) set codes of ``scan_ids``."""
    from omr_scanner.services import review_store
    from omr_scanner.services import set_identity as sets

    wanted, batches = _per_batch(population_, scan_ids)
    loaded = identity if identity is not None else sets.load(database)
    found: dict[int, EffectiveIdentifier] = {}
    for batch_id in batches:
        for scan_id, item in review_store.effective_set_codes(
            database, batch_id, identity=loaded
        ).items():
            if scan_id in wanted:
                found[scan_id] = item
    return found


def effective_answers(
    database: ProjectDatabase,
    population_: SessionPopulation,
    template: OmrTemplate,
    scan_ids: Iterable[int] | None = None,
) -> dict[int, EffectiveAnswers]:
    """Post-review answer decisions of ``scan_ids``."""
    from omr_scanner.services import review_store

    wanted, batches = _per_batch(population_, scan_ids)
    found: dict[int, EffectiveAnswers] = {}
    for batch_id in batches:
        for scan_id, item in review_store.effective_answers(database, batch_id, template).items():
            if scan_id in wanted:
                found[scan_id] = item
    return found


def results_by_scan(
    database: ProjectDatabase,
    population_: SessionPopulation,
    scan_ids: Iterable[int] | None = None,
) -> dict[int, ScanResult]:
    """Stored recognition results of ``scan_ids`` (no image is read)."""
    from omr_scanner.services import batch_store

    wanted, batches = _per_batch(population_, scan_ids)
    found: dict[int, ScanResult] = {}
    for batch_id in batches:
        for scan_id, result in batch_store.results_by_scan(database, batch_id).items():
            if scan_id in wanted:
                found[scan_id] = result
    return found


def describe(population_: SessionPopulation) -> str:
    """One line for a stage header: what the session's population is made of."""
    counts = population_.counts()
    effective = len(population_.effective)
    batches = len(population_.contributing_batch_ids)
    parts = [
        f"{effective} effective script(s) from {batches} batch(es)"
        if batches != 1
        else f"{effective} effective script(s)"
    ]
    replaced = counts.get(SheetDisposition.SUPERSEDED_BY_REPLACEMENT, 0) + counts.get(
        SheetDisposition.BATCH_SUPERSEDED, 0
    )
    if replaced:
        parts.append(f"{replaced} replaced")
    left_out = sum(
        counts.get(kind, 0)
        for kind in (
            SheetDisposition.REJECTED_PENDING_RESCAN,
            SheetDisposition.EXCLUDED,
            SheetDisposition.DEFERRED,
            SheetDisposition.REIMPORT_OF_REJECTED,
        )
    )
    if left_out:
        parts.append(f"{left_out} rejected / excluded / deferred")
    unread = counts.get(SheetDisposition.NOT_READ, 0)
    if unread:
        parts.append(f"{unread} not read yet")
    return " · ".join(parts)


def count_in_session(database: ProjectDatabase, scan_session_id: str) -> int:
    """Rows of the session's batches - for a cheap size check."""
    with database.session() as session:
        batches = [item for item, _c in _session_batches(session, scan_session_id)]
        if not batches:
            return 0
        return int(
            session.scalar(
                select(func.count()).select_from(BatchScan).where(BatchScan.batch_id.in_(batches))
            )
            or 0
        )


__all__ = [
    "LONE_BATCH_PREFIX",
    "SessionPopulation",
    "count_in_session",
    "describe",
    "effective_answers",
    "effective_count",
    "effective_identifiers",
    "effective_set_codes",
    "population",
    "population_key",
    "results_by_scan",
    "session_key",
    "session_of_batch",
    "session_population",
]
