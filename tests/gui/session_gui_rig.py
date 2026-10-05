"""Shared rig for the operational-GUI tests (0.1.1 revised phase 8).

A real project, a real open scan session with watched sources on the fake
filesystem and clock of :mod:`tests.engine_rig`, the real continuous engine
with inline (real) recognition - and the real Scan page, whose session mode
drives that engine through its QThread runner. Nothing here exists in the
application: the engine factory and the snapshot clock are the two injection
points the session-mode controller offers for exactly this.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QApplication
from tests.engine_rig import EngineRig, readable_sheets
from tests.integration.test_session_finish import BIG, resolve_all_conflicts

from omr_scanner.domain.processing import EngineLimits
from omr_scanner.gui import session_close
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services.intake import IntakeService

if TYPE_CHECKING:
    from omr_scanner.gui.scan.session_poller import SessionView

OPERATOR = "Operator"
TIMEOUT_S = 60.0
SHEETS = readable_sheets(12)


class SessionGuiRig(EngineRig):
    """An :class:`EngineRig` plus the Scan page in session mode."""

    def __init__(self, project_session: Any, monkeypatch: Any = None) -> None:
        super().__init__(project_session)
        self.page: ScanPage | None = None
        if monkeypatch is not None:
            # *Finish scan session* outside the engine lists the sources on
            # this rig's fake disk, as the engine's own intake does.
            monkeypatch.setattr(
                session_close,
                "make_intake",
                lambda project: IntakeService(
                    project.database, project.root, fs=self.fs, clock=self.clock, recover=False
                ),
            )

    # --- the engine the GUI starts ---------------------------------------
    def factory(self) -> Callable[[], object]:
        """What the page's runner builds in its thread: this rig's engine, not started."""

        def build() -> object:
            return self.new_engine(
                start=False,
                unit_policy=BIG,
                limits=EngineLimits(max_in_flight=4, claim_window=4),
            )

        return build

    # --- the page --------------------------------------------------------
    def build_page(self, qtbot: Any, *, reviewer: str = OPERATOR) -> ScanPage:
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        page = ScanPage(spec)
        qtbot.addWidget(page)
        page.set_reviewer(reviewer)
        page.state.template = self.template
        mode = page.session_mode
        mode.engine_factory = self.factory()  # type: ignore[assignment]
        mode.poller.clock = self.clock
        page.on_project_changed(self.project)
        page.resize(1366, 768)
        self.page = page
        return page

    def view(self) -> SessionView | None:
        assert self.page is not None
        return self.page.session_mode.view

    def wait_for(
        self, predicate: Callable[[], bool], *, timeout: float = TIMEOUT_S, tick: float = 0.0
    ) -> None:
        """Pump events, refreshing the snapshot, until ``predicate`` holds.

        ``tick``: seconds to advance the fake clock per round (files stabilise
        on it; the engine thread reads it).
        """
        assert self.page is not None
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError("condition not reached")
            if tick:
                self.clock.advance(tick)
            self.page.session_mode.poller.refresh()
            for _ in range(5):
                QApplication.processEvents()
            time.sleep(0.02)

    def processed(self, count: int) -> Callable[[], bool]:
        def check() -> bool:
            view = self.view()
            return view is not None and view.snapshot.recognition.done >= count

        return check

    def resolve_everything(self) -> None:
        resolve_all_conflicts(self)


def wait_stopped(page: ScanPage, *, timeout: float = TIMEOUT_S) -> None:
    """Pump events until the page's engine thread has finished."""
    deadline = time.monotonic() + timeout
    while page.session_mode.running:
        if time.monotonic() > deadline:
            raise TimeoutError("the engine did not stop")
        QApplication.processEvents()
        time.sleep(0.02)
    for _ in range(5):
        QApplication.processEvents()


__all__ = ["OPERATOR", "SHEETS", "SessionGuiRig", "wait_stopped"]
