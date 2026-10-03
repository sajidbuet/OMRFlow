"""GUI test configuration.

Qt needs a platform plugin. On a developer machine the native plugin is used; on
a headless CI runner there is none, so the offscreen plugin is selected
automatically. Tests never depend on a visible window.
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("pytestqt", reason="pytest-qt is required for GUI tests")


def pytest_configure() -> None:
    """Select the offscreen Qt platform when no display is available."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    headless = sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY")
    if headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(autouse=True)
def canonical_interface_zoom():
    """Return the interface to 100% after every GUI test.

    The interface zoom belongs to the `QApplication`, which pytest-qt shares
    across the whole session. A test that zooms to 150% and fails before
    zooming back would otherwise leave every later test measuring a 150%
    chrome row against 100% tokens - a cascade of failures with nothing to
    do with the tests that report them.
    """
    yield
    from omr_scanner.gui.ui_scale import UiScaleManager

    manager = UiScaleManager.find()
    if manager is not None and manager.percent != 100:
        manager.set_percent(100)
