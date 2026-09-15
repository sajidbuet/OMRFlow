"""Tests for `SnapshotHistory`, the undo/redo stack over immutable snapshots."""

from __future__ import annotations

import pytest

from omr_scanner.gui.template_designer.history import SnapshotHistory


class TestBasics:
    def test_a_new_history_starts_at_its_initial_snapshot(self):
        history = SnapshotHistory("a")
        assert history.current == "a"
        assert history.can_undo is False
        assert history.can_redo is False
        assert history.depth == 1

    def test_pushing_advances_the_current_snapshot(self):
        history = SnapshotHistory("a")
        history.push("b")
        assert history.current == "b"
        assert history.can_undo is True
        assert history.can_redo is False

    def test_undo_returns_to_the_previous_snapshot(self):
        history = SnapshotHistory("a")
        history.push("b")
        assert history.undo() == "a"
        assert history.current == "a"

    def test_redo_reapplies_an_undone_snapshot(self):
        history = SnapshotHistory("a")
        history.push("b")
        history.undo()
        assert history.redo() == "b"
        assert history.current == "b"

    def test_undo_with_nothing_to_undo_raises(self):
        history = SnapshotHistory("a")
        with pytest.raises(IndexError, match="Nothing to undo"):
            history.undo()

    def test_redo_with_nothing_to_redo_raises(self):
        history = SnapshotHistory("a")
        with pytest.raises(IndexError, match="Nothing to redo"):
            history.redo()


class TestPushingAfterUndo:
    def test_a_new_push_discards_the_abandoned_redo_future(self):
        history = SnapshotHistory("a")
        history.push("b")
        history.push("c")
        history.undo()  # back to "b"
        history.push("d")  # "c" is now unreachable

        assert history.current == "d"
        assert history.can_redo is False
        with pytest.raises(IndexError):
            history.redo()

    def test_undoing_twice_then_pushing_keeps_only_the_surviving_prefix(self):
        history = SnapshotHistory(0)
        for value in (1, 2, 3):
            history.push(value)
        history.undo()
        history.undo()
        history.push(99)
        assert list(history.all_snapshots()) == [0, 1, 99]


class TestNoOpPushes:
    def test_pushing_the_same_value_as_current_is_a_no_op(self):
        history = SnapshotHistory("a")
        history.push("a")
        assert history.depth == 1
        assert history.can_undo is False


class TestMaxDepth:
    def test_the_oldest_snapshots_are_discarded_beyond_max_depth(self):
        history = SnapshotHistory(0, max_depth=3)
        for value in range(1, 10):
            history.push(value)
        assert history.depth == 3
        assert list(history.all_snapshots()) == [7, 8, 9]
        assert history.current == 9

    def test_a_max_depth_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="max_depth"):
            SnapshotHistory(0, max_depth=0)


class TestReset:
    def test_reset_discards_all_history(self):
        history = SnapshotHistory("a")
        history.push("b")
        history.push("c")
        history.reset("z")
        assert history.current == "z"
        assert history.depth == 1
        assert history.can_undo is False
        assert history.can_redo is False


class TestAllSnapshots:
    def test_returns_every_retained_snapshot_oldest_first(self):
        history = SnapshotHistory("a")
        history.push("b")
        history.push("c")
        history.undo()
        assert list(history.all_snapshots()) == ["a", "b", "c"]
