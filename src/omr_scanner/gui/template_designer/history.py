"""Undo/redo as a snapshot stack over an immutable template.

Purpose:
    Give the designer undo/redo without inventing a command-object hierarchy
    that has to be kept in sync with every possible edit.

Why a snapshot stack works here:
    :class:`~omr_scanner.domain.template.OmrTemplate` is an immutable Pydantic
    model, and every edit in the designer already goes through
    ``template.model_copy(update=...)`` to produce a new one. A snapshot stack
    of those results *is* a correct undo history for free: pushing the new
    document after each completed edit means undo is "go back one document",
    with no risk of a command's `undo()` drifting out of sync with its `redo()`
    as the schema evolves. The document is small (a sheet design is a few
    kilobytes of JSON), so keeping a bounded number of full copies costs
    nothing that matters.

Responsibilities:
    * Push a new document, truncating any redo history past the current point
      (the standard behaviour every undo/redo user expects).
    * Move backward/forward through the stack.
    * Report whether undo/redo is currently possible, for enabling/disabling
      menu actions.

What does NOT belong here:
    * *When* to push. A drag or resize gesture must push exactly once, at
      mouse-release - the caller (the canvas) decides that; this class has no
      concept of a "gesture" or of Qt events at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

DEFAULT_MAX_DEPTH = 100
"""How many snapshots are retained. Bounds memory on a very long editing
session; a designer that needed more than 100 undo steps back would almost
certainly rather start over."""


class SnapshotHistory[T]:
    """A bounded undo/redo stack of immutable snapshots.

    Generic so the same class serves the template designer today and any other
    editor over an immutable document later, without duplicating this logic.

    Args:
        initial: The starting snapshot - what "nothing to undo" reverts to.
        max_depth: Oldest snapshots are discarded beyond this many entries.
    """

    def __init__(self, initial: T, *, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self._snapshots: list[T] = [initial]
        self._index = 0
        self._max_depth = max_depth

    @property
    def current(self) -> T:
        """The snapshot in effect right now."""
        return self._snapshots[self._index]

    @property
    def can_undo(self) -> bool:
        """Whether :meth:`undo` would move to an earlier snapshot."""
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        """Whether :meth:`redo` would move to a later snapshot."""
        return self._index < len(self._snapshots) - 1

    @property
    def depth(self) -> int:
        """How many snapshots are currently retained."""
        return len(self._snapshots)

    def push(self, snapshot: T) -> None:
        """Record a completed edit as the new current snapshot.

        Any redo history beyond the current point is discarded first: once a
        user makes a new edit after undoing, the abandoned "future" can no
        longer be reached, which is the behaviour every undo/redo system uses.

        A snapshot identical to the current one (an edit that produced no
        actual change, such as dragging a handle back to where it started) is
        not pushed, so an undo step is never wasted on a no-op.
        """
        if snapshot == self.current:
            return
        del self._snapshots[self._index + 1 :]
        self._snapshots.append(snapshot)
        if len(self._snapshots) > self._max_depth:
            overflow = len(self._snapshots) - self._max_depth
            del self._snapshots[:overflow]
        self._index = len(self._snapshots) - 1

    def undo(self) -> T:
        """Move to the previous snapshot and return it.

        Raises:
            IndexError: There is nothing to undo (:attr:`can_undo` is False).
        """
        if not self.can_undo:
            raise IndexError("Nothing to undo")
        self._index -= 1
        return self.current

    def redo(self) -> T:
        """Move to the next snapshot and return it.

        Raises:
            IndexError: There is nothing to redo (:attr:`can_redo` is False).
        """
        if not self.can_redo:
            raise IndexError("Nothing to redo")
        self._index += 1
        return self.current

    def reset(self, snapshot: T) -> None:
        """Discard all history and start over from ``snapshot``.

        Used when a *different* document is loaded (File > Open), where the
        previous document's undo history is meaningless.
        """
        self._snapshots = [snapshot]
        self._index = 0

    def all_snapshots(self) -> Sequence[T]:
        """Return every retained snapshot, oldest first - for tests and diagnostics."""
        return tuple(self._snapshots)
