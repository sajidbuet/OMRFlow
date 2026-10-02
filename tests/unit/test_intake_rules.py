"""The intake ledger's vocabulary: transitions, name rules, stabilisation timing.

Pure tests of :mod:`omr_scanner.domain.intake` - every allowed transition is
accepted, every other one refused, and the name classifier and the
"due for verification" rule behave at their edges.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from omr_scanner.domain.intake import (
    INITIAL_STATES,
    LOCAL_POLICY,
    NETWORK_POLICY,
    TRANSITIONS,
    Exclusions,
    IntakeReason,
    IntakeState,
    IntakeTransitionError,
    SourceAction,
    StabilityPolicy,
    classify_name,
    default_policy,
    due_for_verification,
    is_network_path,
    is_temporary_name,
    require_transition,
)
from omr_scanner.services.scan_import import SUPPORTED_SCAN_SUFFIXES

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class TestTransitions:
    def test_the_main_path_is_allowed(self):
        path = [
            (None, IntakeState.DISCOVERED),
            (IntakeState.DISCOVERED, IntakeState.STABILIZING),
            (IntakeState.STABILIZING, IntakeState.READY),
            (IntakeState.READY, IntakeState.REGISTERED),
        ]
        for current, target in path:
            require_transition(current, target)

    @pytest.mark.parametrize(
        "current,target",
        [(current, target) for current, targets in TRANSITIONS.items() for target in targets],
    )
    def test_every_listed_transition_is_allowed(self, current, target):
        require_transition(current, target)

    @pytest.mark.parametrize(
        "current,target",
        [
            (current, target)
            for current, target in product(IntakeState, IntakeState)
            if target not in TRANSITIONS[current]
            and not (current is target and current.is_unsettled)
        ],
    )
    def test_every_other_transition_is_refused(self, current, target):
        with pytest.raises(IntakeTransitionError):
            require_transition(current, target)

    @pytest.mark.parametrize("state", list(IntakeState))
    def test_only_initial_states_can_start_a_row(self, state):
        if state in INITIAL_STATES:
            require_transition(None, state)
        else:
            with pytest.raises(IntakeTransitionError):
                require_transition(None, state)

    def test_terminal_states_have_no_exit_except_registered_to_duplicate(self):
        for state in IntakeState:
            if state.is_terminal and state is not IntakeState.REGISTERED:
                assert TRANSITIONS[state] == frozenset(), state
        assert TRANSITIONS[IntakeState.REGISTERED] == {IntakeState.DUPLICATE_CONTENT}

    def test_vanished_reopens_only_to_discovered(self):
        assert TRANSITIONS[IntakeState.VANISHED] == {IntakeState.DISCOVERED}

    def test_held_has_no_exit_in_phase_5(self):
        assert TRANSITIONS[IntakeState.HELD] == frozenset()

    def test_registration_never_skips_ready(self):
        for state in (IntakeState.DISCOVERED, IntakeState.STABILIZING, IntakeState.VANISHED):
            assert IntakeState.REGISTERED not in TRANSITIONS[state]

    def test_consumed_content(self):
        assert {state for state in IntakeState if state.consumed_content} == {
            IntakeState.REGISTERED,
            IntakeState.DUPLICATE_CONTENT,
        }

    def test_audit_actions_fit_the_ledger_column(self):
        assert all(len(action.value) <= 20 for action in SourceAction)

    def test_reasons_fit_their_column(self):
        assert all(len(reason.value) <= 30 for reason in IntakeReason)

    def test_states_fit_their_column(self):
        assert all(len(state.value) <= 20 for state in IntakeState)


class TestNames:
    @pytest.mark.parametrize(
        "name", ["scan.tmp", "SCAN.TMP", "a.part", "a.jpg.part", "~a.jpg", ".a.jpg", "x.crdownload"]
    )
    def test_temporary_names(self, name):
        assert is_temporary_name(name)
        assert classify_name(
            name, exclusions=Exclusions(), supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is (
            IntakeReason.TEMPORARY_NAME
        )

    @pytest.mark.parametrize(
        "name", ["000001.jpg", "scan 1.PNG", "page.tiff", "a.b.bmp", "ছবি.jpg"]
    )
    def test_supported_names_are_candidates(self, name):
        assert classify_name(
            name, exclusions=Exclusions(), supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is IntakeReason.NONE

    @pytest.mark.parametrize("name", ["scan.pdf", "notes.txt", "Thumbs.db", "noext"])
    def test_unsupported_suffix(self, name):
        assert classify_name(
            name, exclusions=Exclusions(), supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is IntakeReason.UNSUPPORTED_SUFFIX

    def test_configured_file_pattern(self):
        rules = Exclusions(files=("*_preview.jpg",))
        assert classify_name(
            "a/x_PREVIEW.jpg", exclusions=rules, supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is IntakeReason.EXCLUDED_NAME

    def test_configured_folder_pattern(self):
        rules = Exclusions(folders=("archive",))
        assert rules.excludes_folder("archive")
        assert rules.excludes_folder("2026/archive")
        assert rules.excludes_folder("Archive/old")
        assert not rules.excludes_folder("archived")
        assert classify_name(
            "archive/a.jpg", exclusions=rules, supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is IntakeReason.EXCLUDED_FOLDER

    def test_temporary_wins_over_suffix(self):
        assert classify_name(
            "a.jpg.tmp", exclusions=Exclusions(), supported_suffixes=SUPPORTED_SCAN_SUFFIXES
        ) is IntakeReason.TEMPORARY_NAME

    def test_exclusions_round_trip(self):
        rules = Exclusions(files=("*.x",), folders=("old",))
        assert Exclusions.from_json(rules.to_json()) == rules
        assert Exclusions.from_json("") == Exclusions()


class TestPolicy:
    def test_unc_paths_get_the_network_policy(self):
        assert is_network_path(r"\\scanner-a\scans")
        assert is_network_path(r"\\?\UNC\scanner-a\scans")
        assert not is_network_path(r"C:\scans")
        assert not is_network_path(r"\\?\C:\scans")
        assert default_policy(r"\\scanner-a\scans") is NETWORK_POLICY
        assert default_policy(r"D:\exam\scans") is LOCAL_POLICY

    def test_policy_round_trip_and_defaults(self):
        policy = StabilityPolicy(min_observations=3, quiet_seconds=1.5)
        assert StabilityPolicy.from_json(policy.to_json()) == policy
        assert StabilityPolicy.from_json('{"quiet_seconds": 9}').quiet_seconds == 9
        assert StabilityPolicy.from_json(None) is LOCAL_POLICY

    @pytest.mark.parametrize(
        "bad",
        [{"min_observations": 0}, {"quiet_seconds": -1}, {"max_decode_attempts": 0}],
    )
    def test_meaningless_thresholds_are_refused(self, bad):
        with pytest.raises(ValueError):
            StabilityPolicy(**bad)

    def test_backoff_doubles(self):
        policy = StabilityPolicy(retry_backoff_seconds=2)
        assert [policy.backoff(n).total_seconds() for n in (1, 2, 3)] == [2, 4, 8]


class TestDue:
    policy = StabilityPolicy(min_observations=2, quiet_seconds=5)

    def due(self, observations, since_seconds, retry_in=None):
        return due_for_verification(
            observations=observations,
            stable_since=(
                NOW - timedelta(seconds=since_seconds) if since_seconds is not None else None
            ),
            retry_after=NOW + timedelta(seconds=retry_in) if retry_in is not None else None,
            now=NOW,
            policy=self.policy,
        )

    def test_needs_both_observations_and_time(self):
        assert self.due(2, 5)
        assert not self.due(1, 60)
        assert not self.due(5, 4.999)

    def test_reset_timer_is_never_due(self):
        assert not self.due(9, None)

    def test_backoff_blocks(self):
        assert not self.due(3, 60, retry_in=1)
        assert self.due(3, 60, retry_in=0)
