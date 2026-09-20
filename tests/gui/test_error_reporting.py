"""Tests for the exception-presentation module (Phase 10, §41).

`report_error` already has indirect coverage everywhere it is called from a
GUI page; this file covers it directly plus the global fallback hook, which
nothing else exercises.
"""

from __future__ import annotations

import logging
import sys

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget

from omr_scanner.errors import OMRScannerError
from omr_scanner.gui import error_reporting

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _reset_installed_hook() -> None:
    """Every test starts with no hook installed, and the real hook restored after."""
    original_hook = error_reporting._original_excepthook
    original_sys_hook = sys.excepthook
    error_reporting._original_excepthook = None
    yield
    error_reporting._original_excepthook = original_hook
    sys.excepthook = original_sys_hook


@pytest.fixture
def captured_critical_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []

    def fake_critical(_parent: object, title: str, text: str, *_args: object) -> None:
        shown.append((title, text))

    monkeypatch.setattr(error_reporting.QMessageBox, "critical", staticmethod(fake_critical))
    return shown


class TestReportError:
    def test_an_omr_scanner_error_shows_its_user_message(self, monkeypatch: pytest.MonkeyPatch):
        shown: list[tuple[str, str]] = []
        monkeypatch.setattr(
            error_reporting.QMessageBox,
            "warning",
            staticmethod(lambda *a: shown.append((a[1], a[2]))),
        )
        error_reporting.report_error(
            None, OMRScannerError("technical detail", user_message="Nice message"), context="Do it"
        )
        assert shown == [("Do it", "Nice message")]

    def test_an_unexpected_error_shows_the_generic_message(self, monkeypatch: pytest.MonkeyPatch):
        shown: list[tuple[str, str]] = []
        monkeypatch.setattr(
            error_reporting.QMessageBox,
            "warning",
            staticmethod(lambda *a: shown.append((a[1], a[2]))),
        )
        error_reporting.report_error(None, ValueError("boom"), context="Do it")
        assert shown == [("Do it", error_reporting.UNEXPECTED_ERROR_TEXT)]


class TestInstallGlobalExceptionHandler:
    def test_installing_replaces_sys_excepthook(self):
        previous = sys.excepthook
        error_reporting.install_global_exception_handler(None)
        assert sys.excepthook is not previous
        assert error_reporting._original_excepthook is previous

    def test_installing_twice_is_a_no_op(self):
        error_reporting.install_global_exception_handler(None)
        installed_hook = sys.excepthook
        stored_original = error_reporting._original_excepthook
        error_reporting.install_global_exception_handler(None)
        assert sys.excepthook is installed_hook
        assert error_reporting._original_excepthook is stored_original

    def test_an_uncaught_exception_is_logged_and_shown(
        self, captured_critical_boxes: list[tuple[str, str]], caplog: pytest.LogCaptureFixture
    ):
        # A stub "previous hook", not the real `sys.excepthook` - pytest-qt
        # installs its own hook for the duration of every test to catch
        # exceptions raised inside the Qt event loop, and forwarding to it
        # here would make it (correctly) fail this test on the exception
        # this test raises on purpose.
        error_reporting._original_excepthook = lambda _t, _v, _tb: None
        with caplog.at_level(logging.CRITICAL, logger=error_reporting.__name__):
            try:
                raise RuntimeError("slot blew up")
            except RuntimeError:
                exc_type, exc_value, exc_tb = sys.exc_info()
                error_reporting._handle_uncaught_exception(None, exc_type, exc_value, exc_tb)

        assert captured_critical_boxes
        title, text = captured_critical_boxes[0]
        assert title == "Unexpected error"
        assert "may not have been saved" in text
        assert "slot blew up" not in text  # no raw traceback in the dialog
        assert any("Unhandled exception" in record.message for record in caplog.records)

    def test_the_dialog_never_claims_data_was_saved(
        self, captured_critical_boxes: list[tuple[str, str]]
    ):
        error_reporting._original_excepthook = lambda _t, _v, _tb: None
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            error_reporting._handle_uncaught_exception(None, exc_type, exc_value, exc_tb)

        _title, text = captured_critical_boxes[0]
        assert "has been saved" not in text.lower()

    def test_a_keyboard_interrupt_is_forwarded_without_a_dialog(
        self, captured_critical_boxes: list[tuple[str, str]]
    ):
        forwarded: list[BaseException] = []
        error_reporting._original_excepthook = lambda _t, v, _tb: forwarded.append(v)
        # install_global_exception_handler would overwrite the fixture-installed
        # original above, so call the hook directly instead of through install().
        exc = KeyboardInterrupt()
        error_reporting._handle_uncaught_exception(None, KeyboardInterrupt, exc, None)

        assert not captured_critical_boxes
        assert forwarded == [exc]

    def test_the_previous_hook_is_still_invoked_after_handling(
        self, captured_critical_boxes: list[tuple[str, str]]
    ):
        forwarded: list[BaseException] = []
        error_reporting._original_excepthook = lambda _t, v, _tb: forwarded.append(v)
        exc = RuntimeError("boom")
        error_reporting._handle_uncaught_exception(None, RuntimeError, exc, None)
        assert forwarded == [exc]

    def test_a_failing_message_box_does_not_prevent_forwarding(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def raising_critical(*_a: object) -> None:
            raise RuntimeError("Qt is unavailable")

        monkeypatch.setattr(QMessageBox, "critical", staticmethod(raising_critical))
        forwarded: list[BaseException] = []
        error_reporting._original_excepthook = lambda _t, v, _tb: forwarded.append(v)
        exc = RuntimeError("boom")

        error_reporting._handle_uncaught_exception(None, RuntimeError, exc, None)

        assert forwarded == [exc]

    def test_installed_via_a_real_widget_parent(self, qtbot):
        widget = QWidget()
        qtbot.addWidget(widget)
        error_reporting.install_global_exception_handler(widget)
        assert sys.excepthook is not None
