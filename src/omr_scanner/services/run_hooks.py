"""Observation points on a Scan run's durable boundaries (0.1.1 phase 3).

Purpose:
    Let a supervisor *outside* the Scan stage see - and, for a crash test,
    deliberately stop at - the exact moments ADR-0006 defines: a run's work
    submitted to recognition, a sheet one of those committed, recognition of
    the whole run committed, and the batch-scope review state completed.

How it is wired:
    By dependency injection only. :class:`~omr_scanner.gui.scan.page.ScanPage`
    has a ``run_hooks`` attribute that is ``None`` in the application and is
    set by the real-process crash harness
    (``tests/crash/scan_resolve_child.py``) to an object that writes an
    external submission log and, at a chosen boundary, blocks until it is
    killed. No global, no environment variable and no test import is
    consulted by production code; with no hooks the run is byte-for-byte the
    same.

What does NOT belong here:
    Anything that changes what is recognised or written. A hook observes; the
    harness's ability to *pause* is what makes a kill boundary deterministic,
    and the kill itself still terminates a real process from outside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from omr_scanner.services.batch_processor import ProcessedScan


class RunHooks(Protocol):
    """Callbacks at a Scan run's durable boundaries. Every one is optional to act on."""

    def submitted(self, batch_id: str | None, paths: Sequence[Path]) -> None:
        """A run is about to hand ``paths`` to recognition (worker thread)."""

    def started(self, path: Path) -> None:
        """One-worker runs only: ``path`` is about to be read (worker thread)."""

    def committed(self, batch_id: str | None, outcomes: Sequence[ProcessedScan]) -> None:
        """These sheets' durable work units have just committed (worker thread)."""

    def run_recognised(self, batch_id: str | None) -> None:
        """Every result of the run is committed; batch-scope review state is next (GUI thread)."""

    def review_state_completed(self, batch_id: str | None) -> None:
        """Batch-scope review state committed; the batch status is next (GUI thread)."""


__all__ = ["RunHooks"]
