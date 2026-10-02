"""Shared rig for the continuous-engine tests (0.1.1 revised phase 6).

A real project database, a real open scan session, real intake sources on a
fake filesystem and clock (:mod:`tests.intake_fakes`), the stress dataset's
real synthetic sheets, and the real engine. Recognition is the real engine's
too: :class:`CountingRecognise` reads each sheet with
:func:`~omr_scanner.services.recognition_service.recognise_scan` the first time
its *bytes* are seen in the test session and replays that genuine result
afterwards (recognition is deterministic, so the replay is the result a
re-read would produce) - which is what keeps a 1/25/50/75/99 % restart series
fast. Every call is counted per sheet, which is the submission log the
"never resubmitted" tests assert on.

A "kill" in these deterministic tests is :class:`Killed` raised from an
:class:`~omr_scanner.services.continuous_engine.EngineHooks` boundary: it
propagates out of the engine, every open transaction rolls back, and the
engine object is abandoned without a shutdown - only what was committed
survives, as after a real kill. Real process kills are in ``tests/crash``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from tests.crash.harness import template as harness_template
from tests.intake_fakes import FakeClock, FakeFileSystem

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    IntakeFile,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.domain.intake import StabilityPolicy
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.services import batch_store, scan_sessions, session_population
from omr_scanner.services import intake as intake_service
from omr_scanner.services.continuous_engine import Claim, ContinuousEngine, EngineHooks
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.recognition_models import ScanResult
from omr_scanner.services.recognition_pool import InlineRecogniser
from omr_scanner.services.recognition_service import recognise_scan
from omr_scanner.services.recognition_settings import RecognitionOptions

POLICY = StabilityPolicy(
    min_observations=2, quiet_seconds=5, max_decode_attempts=3, retry_backoff_seconds=5,
    poll_interval_seconds=0,
)
OPTIONS = RecognitionOptions(with_preview=False, keep_bubble_measurements=False)
"""The Scan stage's engine options (no previews, no per-bubble evidence)."""

SEED = 42
"""The phase 3 crash matrix's seed: within its first 60 sheets an ambiguous and
a blank Student ID, a duplicate Student ID on a different image (17/18 - two
sources when spread round robin), a byte-identical copy (29), a malformed file
(30), ambiguous and multiple marks and conflict-generating sheets."""


# ----------------------------------------------------------------------
# Sheets and recognition
# ----------------------------------------------------------------------
@cache
def sheet_bytes(count: int, seed: int = SEED) -> tuple[bytes, ...]:
    """The stress dataset's first ``count`` sheets (PNG or deliberately malformed bytes)."""
    from omr_scanner.evaluation import stress_dataset

    spec = stress_dataset.StressDatasetSpec(seed=seed, sheet_count=count)
    template = harness_template()
    out = []
    for index in range(count):
        sheet = stress_dataset.render_sheet_for_index(spec, template, index)
        out.append(sheet.malformed_bytes or sheet.png_bytes or b"")
    return tuple(out)


def readable_sheets(count: int, seed: int = SEED) -> tuple[bytes, ...]:
    """The decodable sheets among the first ``count`` (malformed ones never reach a batch)."""
    import cv2
    import numpy as np

    return tuple(
        data
        for data in sheet_bytes(count, seed)
        if cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED) is not None
    )


_RESULTS: dict[str, ScanResult] = {}
"""Genuine results by content hash, for the whole test session."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CountingRecognise:
    """``(path, template, options) -> ScanResult``: real results, every call counted.

    Attributes:
        calls: Sheet content hash -> how many times recognition was asked for it.
        order: Content hashes in call order.
        fail: Content hashes for which the call raises (a recognition fault).
        delay: Seconds each call sleeps (a slow worker).
        gate: When set, called before each read; may block or raise.
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.order: list[str] = []
        self.fail: set[str] = set()
        self.delay = 0.0
        self.gate: Callable[[str], None] | None = None

    def __call__(self, path: Path, template: Any, options: RecognitionOptions) -> ScanResult:
        data = path.read_bytes()
        key = digest(data)
        self.calls[key] += 1
        self.order.append(key)
        if self.gate is not None:
            self.gate(key)
        if self.delay:
            time.sleep(self.delay)
        if key in self.fail:
            raise RuntimeError("injected recognition fault")
        cached = _RESULTS.get(key)
        if cached is None:
            cached = recognise_scan(path, template, options=options.with_preview_disabled())
            _RESULTS[key] = cached
        return dataclasses.replace(cached, source_path=path)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


class Killed(BaseException):  # noqa: N818 - it is what happens, not an error
    """Raised from a hook to abandon the engine at that exact boundary."""


@dataclasses.dataclass
class KillAt(EngineHooks):
    """Raise :class:`Killed` at the ``count``-th time ``boundary`` is reached (1-based)."""

    boundary: str = ""
    count: int = 1
    seen: Counter[str] = dataclasses.field(default_factory=Counter)
    committed_ids: list[int] = dataclasses.field(default_factory=list)
    submitted_ids: list[int] = dataclasses.field(default_factory=list)

    def _hit(self, name: str) -> None:
        self.seen[name] += 1
        if name == self.boundary and self.seen[name] == self.count:
            raise Killed(name)

    def registered(self, batch_id: str, scan_ids: Sequence[int]) -> None:
        self._hit("registered")

    def claimed(self, claims: Sequence[Claim]) -> None:
        self._hit("claimed")

    def submitted(self, claim: Claim) -> None:
        self.submitted_ids.append(claim.scan_id)
        self._hit("submitted")

    def recognised(self, claim: Claim) -> None:
        self._hit("recognised")

    def before_commit(self, batch_id: str, claims: Sequence[Claim]) -> None:
        self._hit("before_commit")

    def committed(self, batch_id: str, claims: Sequence[Claim]) -> None:
        self.committed_ids.extend(claim.scan_id for claim in claims)
        self._hit("committed")

    def finalising(self, batch_id: str) -> None:
        self._hit("finalising")

    def finalised(self, batch_id: str, status: str) -> None:
        self._hit("finalised")


# ----------------------------------------------------------------------
# The rig
# ----------------------------------------------------------------------
class EngineRig:
    """One project, one open session, watched sources on a fake disk, the engine."""

    def __init__(self, project_session: Any) -> None:
        self.project = project_session
        self.database = project_session.database
        self.clock = FakeClock()
        self.fs = FakeFileSystem(self.clock)
        self.template = harness_template()
        self.recognise = CountingRecognise()
        self.session_id = scan_sessions.create_scan_session(
            self.database, name="Exam", created_by="op"
        ).scan_session_id
        self.sources: dict[str, str] = {}
        self.engine: ContinuousEngine | None = None

    # --- intake ---------------------------------------------------------
    def source(self, name: str) -> str:
        root = rf"\\scanner-{name}\scans"
        self.fs.add_root(root)
        info = intake_service.create_source(
            self.database, label=f"Scanner {name}", root_path=root, created_by="op",
            clock=self.clock, policy=POLICY,
        )
        intake_service.attach_source(
            self.database, info.source_id, self.session_id, actor="op", clock=self.clock
        )
        self.sources[name] = info.source_id
        return info.source_id

    def root(self, name: str) -> str:
        return rf"\\scanner-{name}\scans"

    def write(self, name: str, files: Sequence[tuple[str, bytes]]) -> None:
        for relative, data in files:
            self.fs.write(self.root(name), relative, data)

    def make_ready(self) -> None:
        """Observe every source twice across the quiet period (files become ready)."""
        assert self.engine is not None
        self.engine.poll_intake(force=True)
        self.clock.advance(POLICY.quiet_seconds + 0.1)
        self.engine.poll_intake(force=True)

    # --- engine ---------------------------------------------------------
    def new_engine(
        self,
        *,
        hooks: EngineHooks | None = None,
        limits: EngineLimits | None = None,
        unit_policy: UnitPolicy | None = None,
        per_poll: int = 1,
        recogniser: Any = None,
        start: bool = True,
    ) -> ContinuousEngine:
        """A fresh engine on the same database: what a restarted process builds."""
        self.engine = ContinuousEngine(
            self.database,
            scan_session_id=self.session_id,
            template=self.template,
            recogniser=recogniser
            if recogniser is not None
            else InlineRecogniser(
                self.template, options=OPTIONS, recognise=self.recognise, per_poll=per_poll
            ),
            intake_factory=lambda: IntakeService(
                self.database, self.project.root, fs=self.fs, clock=self.clock
            ),
            limits=limits if limits is not None else EngineLimits(max_in_flight=4, claim_window=4),
            unit_policy=unit_policy if unit_policy is not None else UnitPolicy(
                max_unit_size=10, trickle_seconds=0
            ),
            clock=self.clock,
            hooks=hooks,
            started_by="op",
        )
        if start:
            self.engine.start()
        return self.engine

    def run(self, *, max_steps: int = 100_000) -> int:
        """Run the current engine until it is caught up."""
        assert self.engine is not None
        return self.engine.run_until_idle(max_steps=max_steps, wait=0.0)

    def run_until_killed(self) -> str:
        """Run until a :class:`Killed` hook fires; return the boundary."""
        try:
            self.run()
        except Killed as killed:
            self.engine = None
            return str(killed)
        raise AssertionError("the kill boundary was never reached")


# ----------------------------------------------------------------------
# What a run leaves behind, comparable across projects
# ----------------------------------------------------------------------
_VOLATILE = ("source_path", "elapsed_seconds", "recognised_at", "timings")


def normalised_result(payload: str) -> str:
    """A stored ``result_json`` without the fields that legitimately differ per run."""
    if not payload:
        return ""
    data = json.loads(payload)
    for key in _VOLATILE:
        data.pop(key, None)
    return json.dumps(data, sort_keys=True)


def durable_view(database: Any, scan_session_id: str) -> dict[str, Any]:
    """The authoritative outcome of a session, keyed by sheet content (not ids or paths).

    * ``results``: content -> (status, outcome, identifier, set code, attempts,
      normalised result) for every read sheet;
    * ``effective``: the session's effective sheet set (phase 4's service);
    * ``conflicts``: (content, type, zone, group, state, machine value, related contents);
    * ``unread_duplicates``: contents never read because their bytes repeat;
    * ``pending``: contents not yet completed.
    """
    population = session_population.session_population(database, scan_session_id)
    with database.session() as session:
        rows = session.execute(
            select(
                BatchScan.scan_id, BatchScan.content_sha256, BatchScan.status, BatchScan.outcome,
                BatchScan.identifier_value, BatchScan.set_code_value, BatchScan.attempt_count,
                BatchScan.result_json,
            ).where(BatchScan.batch_id.in_(list(population.batch_ids)))
        ).all()
        content = {int(row[0]): str(row[1]) for row in rows}
        conflicts = session.execute(
            select(
                ReviewConflict.scan_id, ReviewConflict.conflict_type, ReviewConflict.zone_id,
                ReviewConflict.group_key, ReviewConflict.state, ReviewConflict.machine_value,
                ReviewConflict.related_scan_ids,
            ).where(ReviewConflict.batch_id.in_(list(population.batch_ids)))
        ).all()
        ledger_duplicates = {
            str(item)
            for item in session.scalars(
                select(IntakeFile.content_sha256).where(IntakeFile.state == "duplicate_content")
            ).all()
            if item
        }
    terminal = {ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value, ScanJobStatus.FAILED.value}
    results = {
        str(sha): (str(status), str(outcome), str(ident), str(code), int(attempts),
                   normalised_result(str(payload)))
        for _scan, sha, status, outcome, ident, code, attempts, payload in rows
        if str(status) in terminal
    }
    def related(payload: str) -> tuple[str, ...]:
        return tuple(
            sorted(content.get(int(item), "?") for item in payload.split(",") if item.strip())
        )

    return {
        "results": results,
        "effective": sorted(content[scan] for scan in population.effective),
        "conflicts": sorted(
            (content[int(scan)], str(kind), str(zone), str(group), str(state), str(value),
             related(str(rel)))
            for scan, kind, zone, group, state, value, rel in conflicts
        ),
        "unread_duplicates": sorted(
            {str(sha) for _s, sha, status, *_rest in rows if str(status) == "duplicate"}
            | ledger_duplicates
        ),
        "pending": sorted(
            str(sha) for _s, sha, status, *_rest in rows if str(status) not in terminal
            and str(status) != "duplicate"
        ),
    }


def structure(database: Any, scan_session_id: str) -> dict[str, Any]:
    """Sessions, batches, membership and audit counts - what recovery must never change."""
    with database.session() as session:
        batches = session.execute(
            select(ScanBatch.batch_id, ScanBatch.scan_session_id, ScanBatch.role,
                   ScanBatch.sealed_at, ScanBatch.total_scans, ScanBatch.source_id)
            .order_by(ScanBatch.created_at, ScanBatch.batch_id)
        ).all()
        members = session.execute(
            select(BatchScan.batch_id, BatchScan.scan_id, BatchScan.intake_file_id)
            .order_by(BatchScan.scan_id)
        ).all()
        sessions = session.scalars(select(ScanBatch.scan_session_id).distinct()).all()
        actions = Counter(
            str(item) for item in session.scalars(select(AuditEvent.action)).all()
        )
    return {
        "batches": [tuple(str(value) for value in row) for row in batches],
        "members": [tuple(row) for row in members],
        "sessions": sorted(str(item) for item in sessions),
        "audit_actions": dict(actions),
        "session": scan_session_id,
    }


def submissions_of_committed(rig: EngineRig, committed_contents: set[str]) -> dict[str, int]:
    """For contents committed earlier: how many times recognition was asked for each."""
    return {key: rig.recognise.calls[key] for key in committed_contents}


def committed_contents(database: Any) -> set[str]:
    terminal = [ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value, ScanJobStatus.FAILED.value]
    with database.session() as session:
        return {
            str(item)
            for item in session.scalars(
                select(BatchScan.content_sha256).where(BatchScan.status.in_(terminal))
            ).all()
        }


def status_counts(database: Any) -> dict[str, int]:
    with database.session() as session:
        return dict(Counter(str(item) for item in session.scalars(select(BatchScan.status)).all()))


__all__ = [
    "OPTIONS",
    "POLICY",
    "CountingRecognise",
    "EngineRig",
    "KillAt",
    "Killed",
    "batch_store",
    "committed_contents",
    "digest",
    "durable_view",
    "normalised_result",
    "readable_sheets",
    "sheet_bytes",
    "status_counts",
    "structure",
    "submissions_of_committed",
]
