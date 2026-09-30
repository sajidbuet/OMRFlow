"""A frozen interface leaves a stack dump instead of nothing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QApplication

from omr_scanner.gui.hang_watchdog import HangWatchdog

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.gui


def _block_the_gui_thread_for(seconds: float) -> None:
    """Stand-in for a freeze: the GUI thread busy, its event loop not running."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.05)


def test_a_stall_writes_every_threads_stack(qtbot, tmp_path: Path):
    watchdog = HangWatchdog(tmp_path, threshold_seconds=1.0)
    watchdog.start()
    try:
        qtbot.wait(600)  # a few heartbeats
        _block_the_gui_thread_for(2.8)
        qtbot.waitUntil(lambda: bool(watchdog.reports), timeout=5_000)
        report = watchdog.reports[0].read_text(encoding="utf-8")
        assert "not responding" in report
        # The frozen line is named: the blocking helper above, on the main thread.
        assert "_block_the_gui_thread_for" in report
        assert "MainThread" in report or "Current thread" in report
    finally:
        watchdog.stop()


def test_one_report_per_stall_and_recovery_is_noticed(qtbot, tmp_path: Path, caplog):
    watchdog = HangWatchdog(tmp_path, threshold_seconds=1.0)
    watchdog.start()
    try:
        qtbot.wait(600)
        _block_the_gui_thread_for(3.5)
        qtbot.wait(1200)  # events flow again
        assert len(watchdog.reports) == 1
        assert any("responding again" in record.message for record in caplog.records)
    finally:
        watchdog.stop()


def test_a_responsive_interface_writes_nothing(qtbot, tmp_path: Path):
    watchdog = HangWatchdog(tmp_path, threshold_seconds=1.0)
    watchdog.start()
    try:
        for _ in range(30):
            QApplication.processEvents()
            qtbot.wait(100)
        assert watchdog.reports == []
        assert list(tmp_path.iterdir()) == []
    finally:
        watchdog.stop()
