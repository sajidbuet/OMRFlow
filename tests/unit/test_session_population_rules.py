"""The pure effective-set rules (0.1.1 phase 4): one disposition per sheet.

Scope:
    :func:`omr_scanner.domain.session_population.classify` and
    :func:`~omr_scanner.domain.session_population.lineage_roots`, with no
    database. The service tests (``tests/integration/test_session_population.py``)
    exercise the same rules on persisted state.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.session_population import (
    READ_STATUSES,
    SheetDisposition,
    SheetFacts,
    classify,
    lineage_roots,
)

D = SheetDisposition
S = "session-1"


def facts(**kwargs: object) -> SheetFacts:
    values: dict[str, object] = {"scan_id": 1, "batch_id": "b1", "status": "completed"}
    values.update(kwargs)
    return SheetFacts(**values)  # type: ignore[arg-type]


class TestClassify:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("completed", D.EFFECTIVE),
            ("warning", D.EFFECTIVE),
            ("failed", D.EFFECTIVE_UNREADABLE),
            ("pending", D.NOT_READ),
            ("queued", D.NOT_READ),
            ("processing", D.NOT_READ),
            ("cancelled", D.NOT_READ),
        ],
    )
    def test_an_active_sheet_counts_once_it_was_read(self, status, expected):
        assert classify(facts(status=status), S) is expected

    @pytest.mark.parametrize(
        ("lifecycle", "expected"),
        [
            ("superseded_by_replacement", D.SUPERSEDED_BY_REPLACEMENT),
            ("rejected_pending_rescan", D.REJECTED_PENDING_RESCAN),
            ("reimport_of_rejected", D.REIMPORT_OF_REJECTED),
            ("excluded", D.EXCLUDED),
            ("deferred", D.DEFERRED),
            ("a_state_from_a_newer_build", D.REJECTED_PENDING_RESCAN),
        ],
    )
    def test_a_lifecycle_state_decides_over_the_read_status(self, lifecycle, expected):
        assert classify(facts(lifecycle=lifecycle), S) is expected
        assert classify(facts(lifecycle=lifecycle, status="pending"), S) is expected
        assert not expected.counts

    def test_a_superseded_batch_wins_over_everything(self):
        assert classify(facts(batch_superseded=True, lifecycle="excluded"), S) is (
            D.BATCH_SUPERSEDED
        )

    def test_a_rescan_counts_in_its_lineages_session(self):
        assert classify(facts(counting_session="session-2"), S) is D.COUNTED_IN_OTHER_SESSION
        assert classify(facts(counting_session=S), S) is D.EFFECTIVE

    def test_counted_and_listed_are_disjoint_and_cover_the_attendance_view(self):
        counted = {item for item in D if item.counts}
        listed = {item for item in D if item.is_listed}
        assert counted == {D.EFFECTIVE, D.EFFECTIVE_UNREADABLE}
        assert listed == {D.REJECTED_PENDING_RESCAN, D.DEFERRED}
        assert not counted & listed
        assert all(item.label for item in D)

    def test_read_statuses(self):
        assert frozenset({"completed", "warning", "failed"}) == READ_STATUSES


class TestLineage:
    def test_no_links(self):
        found = lineage_roots({})
        assert found.root_of == {} and found.cycles == frozenset()

    def test_a_single_rescan(self):
        assert lineage_roots({2: 1}).root_of == {2: 1, 1: 1}

    def test_three_generations(self):
        found = lineage_roots({2: 1, 3: 2})
        assert found.root_of == {1: 1, 2: 1, 3: 1}
        assert not found.cycles

    def test_independent_lineages_keep_their_own_roots(self):
        found = lineage_roots({2: 1, 3: 2, 20: 10})
        assert found.root_of[3] == 1 and found.root_of[20] == 10

    def test_order_of_links_does_not_matter(self):
        links = {5: 4, 4: 3, 3: 2, 2: 1}
        reordered = dict(reversed(list(links.items())))
        assert lineage_roots(links) == lineage_roots(reordered)

    def test_a_cycle_is_reported_with_no_root(self):
        found = lineage_roots({1: 2, 2: 1})
        assert found.cycles == frozenset({1, 2})
        assert 1 not in found.root_of and 2 not in found.root_of

    def test_a_chain_into_a_cycle_has_no_root_either(self):
        found = lineage_roots({1: 2, 2: 3, 3: 2})
        assert found.cycles == frozenset({1, 2, 3})

    def test_a_long_chain_is_walked_without_recursion(self):
        size = 50_000
        found = lineage_roots({index + 1: index for index in range(size)})
        assert found.root_of[size] == 0
