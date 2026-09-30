"""The dashboard's analytics count exactly the scripts the Results stage scores.

Scope:
    The Reject & Rescan project (one batch, Sets 1-3, real reconciliation,
    verified keys, real scoring), analysed through the same path the Results
    page's Dashboard tab uses: ``list_results`` for every roster ->
    ``build_records`` -> ``analyse``. Each lifecycle event that changes which
    script counts - a rejection, a rescan, a duplicate, a Resolve correction of
    the set code - is applied through the services and the analytics are
    checked to follow it, and to reconcile with the Results summary.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update
from tests.integration.test_reject_and_rescan import (
    OPERATOR,
    PLAN,
    TEMPLATE,
    World,
    build_world,
    make_result,
    reject,
)

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.review import FieldKind, MachineObservation, ReasonCode
from omr_scanner.services import create_project, review_store, scan_lifecycle, scoring_store
from omr_scanner.services import result_analytics as ra

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def world(workspace: Path, tmp_path: Path) -> Iterator[World]:
    session = create_project(workspace, "Analytics Inclusion")
    try:
        yield build_world(session, tmp_path)
    finally:
        if not session.is_closed:
            session.close()


def score_all(world: World) -> None:
    for code in "123":
        world.reconcile(code)
        world.score(code)


def every_result(world: World) -> list[scoring_store.StoredResult]:
    """What the Results page's table holds: every roster, one read each."""
    return [
        item
        for code in "123"
        for item in scoring_store.list_results(
            world.database, world.rosters[code], world.batch_id, TEMPLATE
        )
    ]


def analyse(world: World) -> ra.ExamAnalytics:
    results = every_result(world)
    keys, policies = ra.load_keys_and_policies(world.database, results)
    records = ra.build_records(results, keys, policies)
    return ra.analyse(
        records, set_codes=("1", "2", "3"), labels=PLAN.labels,
        maximum_possible=float(PLAN.question_count),
    )


def marks(scope: ra.ScopeAnalytics | None) -> list[float]:
    assert scope is not None and scope.summary is not None
    return sorted([scope.summary.minimum, scope.summary.maximum])


class TestReconcilesWithResults:
    def test_counts_match_the_results_summary(self, world):
        score_all(world)
        counts = scoring_store.summarise(every_result(world))
        found = analyse(world)
        assert found.overall.count == counts.scored
        for code, total in counts.by_set.items():
            assert found.scope(code).count == total

    def test_only_scored_candidates_count(self, world):
        score_all(world)
        found = analyse(world)
        # Set 1: 12 and 13 right; Set 2: 14 and 15 (200123 wrote a short roll
        # and cannot be scored, 200124 is absent); Set 3: 17 and 18.
        assert found.scope("1").count == 2
        assert found.scope("2").count == 2
        assert found.scope("3").count == 2
        assert marks(found.scope("2")) == [14.0, 15.0]
        assert found.overall.count == 6

    def test_question_statistics_follow_the_effective_answers(self, world):
        score_all(world)
        scope = analyse(world).scope("1")
        # Both Set 1 scripts answer the first 12 questions "A" (the key).
        assert scope.question(PLAN.numbers[0]).p_correct == 1.0
        assert scope.question(PLAN.numbers[12]).p_correct == 0.5
        assert scope.question(PLAN.numbers[13]).p_correct == 0.0


class TestLifecycle:
    def test_a_rejected_sheet_stops_counting(self, world):
        score_all(world)
        reject(world, "s2b.png")
        score_all(world)
        found = analyse(world)
        assert found.scope("2").count == 1
        assert marks(found.scope("2")) == [14.0, 14.0]

    def test_a_confirmed_rescan_counts_once_with_its_own_answers(self, world):
        score_all(world)
        reject(world, "s2b.png")
        rescan = world.add_processed_scan("IMG_0042.jpg", "200122", "2", 20)
        scan_lifecycle.confirm_replacement(
            world.database, world.ids["s2b.png"], rescan, reviewer=OPERATOR
        )
        score_all(world)
        found = analyse(world)
        assert found.scope("2").count == 2
        assert marks(found.scope("2")) == [14.0, 20.0]

    def test_an_unresolved_duplicate_does_not_count_twice(self, world):
        score_all(world)
        world.add_processed_scan("s2a_again.png", "200121", "2", 3)
        score_all(world)
        found = analyse(world)
        # 200121 has two scripts and none nominated: blocked, so not scored.
        assert found.scope("2").count == 1

    def test_a_kept_duplicate_counts_once(self, world):
        score_all(world)
        again = world.add_processed_scan("s2a_again.png", "200121", "2", 3)
        scan_lifecycle.keep_script(
            world.database, world.ids["s2a.png"], [again], reviewer=OPERATOR,
            candidate_id="200121",
        )
        score_all(world)
        found = analyse(world)
        assert found.scope("2").count == 2
        assert marks(found.scope("2")) == [14.0, 15.0]


class TestResolvedSetCode:
    def test_a_corrected_set_code_is_analysed_under_the_corrected_set(self, world):
        # 300121's sheet was read as Set 2; its candidate is on Set 3's list.
        scan_id = world.ids["s3a.png"]
        result = make_result(world.scans_dir / "s3a.png", "300121", "2", 17)
        with world.database.session() as session:
            session.execute(
                update(BatchScan)
                .where(BatchScan.scan_id == scan_id)
                .values(set_code_value="2", result_json=json.dumps(result.to_dict()))
            )
        score_all(world)
        before = analyse(world)
        assert before.scope("3").count == 1

        review_store.correct_field(
            world.database, batch_id=world.batch_id, scan_id=scan_id, zone_id="set_code",
            values={0: "3"}, display_value="3", field_label="Set", reviewer=OPERATOR,
            reason=ReasonCode.MISCLASSIFICATION,
            overrides={0: MachineObservation(value="2", status="complete", confidence=0.9)},
            field_kind=FieldKind.SET_CODE, previous_value="2",
        )
        score_all(world)
        after = analyse(world)
        assert after.scope("3").count == 2
        assert after.scope("2").count == 2
        assert marks(after.scope("3")) == [17.0, 18.0]
        record_codes = {
            item.candidate_id: item.set_code
            for item in every_result(world)
            if item.final_score == Fraction(17)
        }
        assert record_codes == {"300121": "3"}


class TestStale:
    def test_a_policy_change_is_reported_not_hidden(self, world):
        score_all(world)
        from omr_scanner.domain.scoring import ScoringPolicy

        scoring_store.save_policy(world.database, ScoringPolicy(correct_mark=Fraction(2)))
        found = analyse(world)
        assert found.stale_count == found.overall.count == 6
