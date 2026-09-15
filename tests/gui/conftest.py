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
