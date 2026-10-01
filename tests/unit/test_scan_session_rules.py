"""Scan-session lifecycle rules, as pure values (0.1.1 phase 2).

Covers :mod:`omr_scanner.domain.scan_sessions`: states and transitions, batch
membership and roles, the supersession rules, and the upgrade-backfill
grouping rule. No database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omr_scanner.domain.scan_sessions import (
    BatchFacts,
    BatchMembership,
    BatchRole,
    LegacyBatch,
    ReplacementLink,
    ScanSessionState,
    attach_problem,
    close_problem,
    effective_batches,
    plan_backfill,
    reopen_problem,
    supersession_cycles,
    supersession_problem,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SAME = ("t", "g", "r", "e")


class TestStates:
    def test_open_accepts_batches_and_closed_does_not(self) -> None:
        assert ScanSessionState.OPEN.accepts_batches
        assert not ScanSessionState.CLOSED.accepts_batches
        assert attach_problem(ScanSessionState.OPEN) == ""
        assert "closed" in attach_problem(ScanSessionState.CLOSED)

    def test_close_and_reopen_are_the_only_transitions(self) -> None:
        assert close_problem(ScanSessionState.OPEN) == ""
        assert close_problem(ScanSessionState.CLOSED)
        assert reopen_problem(ScanSessionState.CLOSED) == ""
        assert reopen_problem(ScanSessionState.OPEN)


class TestMembershipAndRoles:
    def test_membership_derives_from_sealed_at(self) -> None:
        assert BatchMembership.of(None) is BatchMembership.OPEN
        assert BatchMembership.of(T0) is BatchMembership.SEALED
        assert BatchMembership.OPEN.accepts_members
        assert not BatchMembership.SEALED.accepts_members

    @pytest.mark.parametrize(
        ("role", "primary"),
        [
            (BatchRole.SCAN, True),
            (BatchRole.RESCAN, False),
            (BatchRole.REPROCESS, True),
            (BatchRole.LEGACY, True),
        ],
    )
    def test_roles(self, role: BatchRole, primary: bool) -> None:
        assert role.is_primary is primary
        assert role.label


def _facts(batch_id: str, scan_session: str | None = "S", *, sealed: bool = True) -> BatchFacts:
    return BatchFacts(batch_id, scan_session, sealed)


class TestSupersession:
    def test_a_valid_supersession(self) -> None:
        assert supersession_problem(_facts("A"), _facts("B", sealed=False), {}) == ""

    def test_a_batch_cannot_supersede_itself(self) -> None:
        assert "itself" in supersession_problem(_facts("A"), _facts("A"), {})

    def test_cross_session_is_refused(self) -> None:
        assert "same scan session" in supersession_problem(_facts("A", "S1"), _facts("B", "S2"), {})

    def test_only_a_sealed_batch_can_be_superseded(self) -> None:
        assert "sealed" in supersession_problem(_facts("A", sealed=False), _facts("B"), {})

    def test_two_live_superseders_are_refused(self) -> None:
        assert "already superseded" in supersession_problem(_facts("A"), _facts("C"), {"A": "B"})

    def test_a_two_cycle_is_refused(self) -> None:
        assert "cycle" in supersession_problem(_facts("B"), _facts("A"), {"A": "B"})

    def test_a_longer_cycle_is_refused(self) -> None:
        live = {"A": "B", "B": "C"}
        assert "cycle" in supersession_problem(_facts("C"), _facts("A"), live)

    def test_a_chain_is_legal(self) -> None:
        assert supersession_problem(_facts("B"), _facts("C"), {"A": "B"}) == ""
        assert effective_batches(["A", "B", "C"], {"A": "B", "B": "C"}) == ("C",)

    def test_cycles_are_found_for_health(self) -> None:
        assert supersession_cycles({"A": "B", "B": "A"})
        assert supersession_cycles({"A": "B", "B": "C"}) == ()


def _legacy(batch_id: str, hours: int, identity: tuple[str, str, str, str] = SAME) -> LegacyBatch:
    return LegacyBatch(batch_id, T0 + timedelta(hours=hours), identity)


class TestBackfillPlan:
    def test_one_batch_is_one_session(self) -> None:
        plan = plan_backfill([_legacy("b1", 0)], [])
        assert plan.groups == (("b1",),)
        assert plan.ambiguous == ()

    def test_unrelated_batches_are_never_combined(self) -> None:
        plan = plan_backfill([_legacy("b1", 0), _legacy("b2", 1)], [])
        assert plan.groups == (("b1",), ("b2",))

    def test_an_unambiguous_cross_batch_rescan_groups_two_batches(self) -> None:
        plan = plan_backfill(
            [_legacy("b1", 0), _legacy("b2", 1)], [ReplacementLink("b1", "b2")] * 2
        )
        assert plan.groups == (("b1", "b2"),)
        assert plan.ambiguous == ()

    def test_three_chained_batches_are_ambiguous(self) -> None:
        plan = plan_backfill(
            [_legacy("b1", 0), _legacy("b2", 1), _legacy("b3", 2)],
            [ReplacementLink("b1", "b2"), ReplacementLink("b2", "b3")],
        )
        assert plan.groups == (("b1",), ("b2",), ("b3",))
        assert len(plan.ambiguous) == 1
        assert "3 batches are chained" in plan.ambiguous[0]

    def test_links_both_ways_are_ambiguous(self) -> None:
        plan = plan_backfill(
            [_legacy("b1", 0), _legacy("b2", 1)],
            [ReplacementLink("b1", "b2"), ReplacementLink("b2", "b1")],
        )
        assert plan.groups == (("b1",), ("b2",))
        assert "both directions" in plan.ambiguous[0]

    def test_an_older_replacement_is_ambiguous(self) -> None:
        plan = plan_backfill([_legacy("b1", 1), _legacy("b2", 0)], [ReplacementLink("b1", "b2")])
        assert plan.groups == (("b2",), ("b1",))
        assert "not newer" in plan.ambiguous[0]

    def test_a_template_difference_is_ambiguous(self) -> None:
        plan = plan_backfill(
            [_legacy("b1", 0), _legacy("b2", 1, ("t", "other", "r", "e"))],
            [ReplacementLink("b1", "b2")],
        )
        assert len(plan.groups) == 2
        assert "template" in plan.ambiguous[0]

    def test_a_link_to_an_unknown_batch_is_ignored(self) -> None:
        plan = plan_backfill([_legacy("b1", 0)], [ReplacementLink("b1", "elsewhere")])
        assert plan.groups == (("b1",),)
