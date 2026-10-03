"""GUI test configuration.

Qt needs a platform plugin. On a developer machine the native plugin is used; on
a headless CI runner there is none, so the offscreen plugin is selected
automatically. Tests never depend on a visible window.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest

pytest.importorskip("pytestqt", reason="pytest-qt is required for GUI tests")


def pytest_configure() -> None:
    """Select the offscreen Qt platform when no display is available."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    headless = sys.platform not in {"win32", "darwin"} and not os.environ.get("DISPLAY")
    if headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"


_WINDOWS_BEFORE = pytest.StashKey[set[int]]()


def _top_level_windows() -> dict[int, object]:
    """The application's live top-level widgets, keyed by C++ address."""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return {}
    return {
        shiboken6.getCppPointer(widget)[0]: widget
        for widget in app.topLevelWidgets()
        if shiboken6.isValid(widget)
    }


def _discard_windows_since(before: set[int]) -> None:
    """Delete every top-level widget created since ``before`` was taken.

    Hidden and deleted rather than closed: a window's ``closeEvent`` may ask
    a question, and a modal prompt here would hang the suite.
    """
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    for address, widget in _top_level_windows().items():
        if address not in before and shiboken6.isValid(widget):
            widget.hide()  # type: ignore[attr-defined]
            widget.deleteLater()  # type: ignore[attr-defined]
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True, scope="module")
def _discard_module_windows() -> Iterator[None]:
    """Delete the windows a module-scoped fixture built, once the module ends."""
    before = set(_top_level_windows())
    yield
    _discard_windows_since(before)


@pytest.fixture(autouse=True)
def _remember_windows(request: pytest.FixtureRequest) -> None:
    """Note the windows alive before the test, after module fixtures exist."""
    request.node.stash[_WINDOWS_BEFORE] = set(_top_level_windows())


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item):
    """Really delete the windows the test built, once it is torn down.

    pytest-qt closes every widget registered with ``qtbot`` and calls
    ``deleteLater()`` on it, but then only runs ``processEvents()``, which does
    not deliver deferred deletes outside an event loop; and a window a test
    built without registering it is never closed at all. Either way it stays
    alive for the rest of the session - tens of thousands of widgets by the
    end of ``tests/gui``, every one of which an interface-zoom change
    re-polishes: gigabytes of memory, and an access violation inside
    ``QApplication.setStyleSheet``.
    """
    result = yield
    before = item.stash.get(_WINDOWS_BEFORE, None)
    if before is not None:
        _discard_windows_since(before)
    else:
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


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
