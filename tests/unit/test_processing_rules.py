"""The continuous engine's pure rules and boundaries (0.1.1 revised phase 6).

* :func:`~omr_scanner.domain.processing.plan_units` - which source forms the
  next finite unit, and how many files it takes;
* :class:`~omr_scanner.domain.processing.EngineLimits` / ``UnitPolicy``
  validation;
* the claim order across batches, and the state transitions written;
* X3: worker-side modules never import the database.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from omr_scanner.database.models import BatchScan, BatchStatus, ScanBatch, ScanJobStatus
from omr_scanner.domain.processing import (
    EngineLimits,
    EngineState,
    PlannedUnit,
    UnitCandidate,
    UnitPolicy,
    plan_units,
)
from omr_scanner.services import batch_store, scan_sessions
from omr_scanner.services.continuous_engine import claim_scans, release_claims

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def candidate(source: str, *, waited: float, first_id: int, count: int) -> UnitCandidate:
    return UnitCandidate(source, NOW - timedelta(seconds=waited), first_id, count)


class TestPlanUnits:
    POLICY = UnitPolicy(max_unit_size=10, trickle_seconds=30)

    def test_a_full_unit_forms_at_once(self):
        plan = plan_units([candidate("a", waited=0, first_id=1, count=10)], now=NOW,
                          policy=self.POLICY, limit=5)
        assert plan == (PlannedUnit("a", 10, True),)

    def test_a_partial_unit_waits_for_the_trickle_timeout(self):
        young = [candidate("a", waited=29.9, first_id=1, count=3)]
        assert plan_units(young, now=NOW, policy=self.POLICY, limit=5) == ()
        old = [candidate("a", waited=30, first_id=1, count=3)]
        assert plan_units(old, now=NOW, policy=self.POLICY, limit=5) == (
            PlannedUnit("a", 3, False),
        )

    def test_a_large_backlog_yields_full_units_and_judges_the_rest_later(self):
        plan = plan_units([candidate("a", waited=999, first_id=1, count=25)], now=NOW,
                          policy=self.POLICY, limit=5)
        assert plan == (PlannedUnit("a", 10, True), PlannedUnit("a", 10, True))

    def test_sources_in_the_ledgers_stable_order(self):
        cands = [
            candidate("b", waited=40, first_id=7, count=2),
            candidate("a", waited=50, first_id=9, count=2),
            candidate("c", waited=40, first_id=3, count=2),
        ]
        plan = plan_units(cands, now=NOW, policy=self.POLICY, limit=5)
        assert [unit.source_id for unit in plan] == ["a", "c", "b"]
        # The same candidates in any input order plan the same units.
        assert plan_units(list(reversed(cands)), now=NOW, policy=self.POLICY, limit=5) == plan

    def test_the_per_pass_limit_bounds_registration(self):
        cands = [candidate(s, waited=99, first_id=i, count=50) for i, s in enumerate("abc")]
        assert len(plan_units(cands, now=NOW, policy=self.POLICY, limit=2)) == 2
        assert plan_units(cands, now=NOW, policy=self.POLICY, limit=1) == (
            PlannedUnit("a", 10, True),
        )

    def test_zero_trickle_forms_whatever_is_ready(self):
        policy = UnitPolicy(max_unit_size=10, trickle_seconds=0)
        assert plan_units([candidate("a", waited=0, first_id=1, count=1)], now=NOW,
                          policy=policy, limit=1) == (PlannedUnit("a", 1, False),)


class TestLimits:
    @pytest.mark.parametrize(
        "field", ["max_in_flight", "claim_window", "max_commit_group", "max_units_per_poll",
                  "writer_retry_limit"],
    )
    def test_a_bound_must_allow_progress(self, field):
        with pytest.raises(ValueError, match=field):
            EngineLimits(**{field: 0})

    def test_negative_retries_refused(self):
        with pytest.raises(ValueError, match="infrastructure_retries"):
            EngineLimits(infrastructure_retries=-1)

    def test_unit_policy_validation(self):
        with pytest.raises(ValueError):
            UnitPolicy(max_unit_size=0)
        with pytest.raises(ValueError):
            UnitPolicy(trickle_seconds=-1)

    def test_limits_sized_for_a_pool(self):
        limits = EngineLimits.for_workers(8)
        assert limits.max_in_flight == 32
        assert limits.claim_window == 32

    def test_engine_states(self):
        assert {state.value for state in EngineState} == {
            "new", "running", "stopping", "stopped", "faulted"
        }


def _batches(project_session, answer_sheet_template, tmp_path: Path, sizes) -> list[str]:
    identity = batch_store.BatchIdentity.of(answer_sheet_template)
    session_id = scan_sessions.create_scan_session(
        project_session.database, name="Exam", created_by="op"
    ).scan_session_id
    made = []
    for unit, size in enumerate(sizes):
        paths = [tmp_path / f"u{unit}" / f"{index}.png" for index in range(size)]
        made.append(
            scan_sessions.start_batch(
                project_session.database, paths, identity=identity, scan_session_id=session_id
            )
        )
    return made


class TestClaims:
    def test_claims_follow_batch_order_then_batch_index(
        self, project_session, answer_sheet_template, tmp_path
    ):
        first, second = _batches(project_session, answer_sheet_template, tmp_path, (3, 3))
        claims = claim_scans(project_session.database, [second, first], 4)
        assert [(item.batch_id, item.batch_index) for item in claims] == [
            (second, 0), (second, 1), (second, 2), (first, 0)
        ]
        with project_session.database.session() as session:
            rows = {
                row.scan_id: row.status for row in session.scalars(select(BatchScan)).all()
            }
            statuses = dict(session.execute(select(ScanBatch.batch_id, ScanBatch.status)).all())
        assert sum(status == ScanJobStatus.PROCESSING.value for status in rows.values()) == 4
        # The claim and its batches' running status commit together.
        assert statuses[first] == statuses[second] == BatchStatus.RUNNING.value

    def test_a_claimed_or_finished_row_is_never_claimed_again(
        self, project_session, answer_sheet_template, tmp_path
    ):
        (only,) = _batches(project_session, answer_sheet_template, tmp_path, (3,))
        database = project_session.database
        first = claim_scans(database, [only], 2)
        second = claim_scans(database, [only], 5)
        assert {item.scan_id for item in first}.isdisjoint({item.scan_id for item in second})
        assert len(second) == 1
        assert claim_scans(database, [only], 5) == ()
        # Releasing returns exactly the claimed rows; a finished row stays finished.
        with database.session() as session:
            row = session.get(BatchScan, second[0].scan_id)
            assert row is not None
            row.status = ScanJobStatus.COMPLETED.value
        assert release_claims(database, [item.scan_id for item in first + second]) == 2
        again = claim_scans(database, [only], 5)
        assert {item.scan_id for item in again} == {item.scan_id for item in first}


WORKER_SIDE = ("recognition_pool.py", "parallel_batch.py")
FORBIDDEN = ("sqlalchemy", "omr_scanner.database", "omr_scanner.services.batch_store",
             "omr_scanner.services.continuous_engine", "omr_scanner.services.intake")


@pytest.mark.parametrize("module", WORKER_SIDE)
def test_worker_side_modules_never_reach_the_database(module):
    """X3: what runs in (or feeds) a worker process cannot open the project database."""
    source = Path(__file__).resolve().parents[2] / "src" / "omr_scanner" / "services" / module
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offending = sorted(
        name for name in imported for prefix in FORBIDDEN
        if name == prefix or name.startswith(prefix + ".")
    )
    assert not offending, offending
