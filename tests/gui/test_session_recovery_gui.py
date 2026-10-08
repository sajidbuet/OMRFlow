"""Reopening a project whose continuous session was interrupted (0.1.1 revised phase 8).

ACCEPTANCE_CRITERIA F8 / ARCHITECTURE_NOTES §13.7: the Scan stage shows the
reconstructed state - per session and source, read / pending, conflicts -
from committed rows **before** anything is started; the same session and the
same finite batches are kept (no recovery session or batch is created); the
persisted processing intent is shown; Resolve's queue is available without
visiting Scan; nothing stored in the GUI is used.

The interruption is a **real kill**: a child process (the phase 7 crash
harness) runs the engine on real folders and is killed with
``TerminateProcess`` inside a commit; the project is then opened in the real
main window.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import func, select
from tests.crash.harness import kill, wait_for
from tests.crash.test_phase7_kills import SHEETS, launch, paused, prepare, read
from tests.engine_rig import digest

from omr_scanner.config import AppConfig
from omr_scanner.database.models import BatchScan, ScanBatch, ScanSession
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.session_controls import ProcessingIntent
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.services import load_template, resolve_active_template, session_controls
from omr_scanner.services.continuous_engine import ContinuousEngine
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.recognition_pool import InlineRecogniser

pytestmark = pytest.mark.gui

READ = ("completed", "warning", "failed")


def committed(root: Path) -> int:
    return int(
        read(root, select(func.count()).select_from(BatchScan).where(BatchScan.status.in_(READ)))[
            0
        ][0]
    )


def structure(root: Path) -> tuple[int, int]:
    sessions = int(read(root, select(func.count()).select_from(ScanSession))[0][0])
    batches = int(read(root, select(func.count()).select_from(ScanBatch))[0][0])
    return sessions, batches


def open_window(qtbot, tmp_path: Path, root: Path) -> MainWindow:
    window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(window)
    window.apply_reviewer_name("Operator")
    assert window.open_project_resolving_lock(root, action="force")
    QApplication.processEvents()
    return window


def inline_factory(window: MainWindow, session_id: str):  # type: ignore[no-untyped-def]
    project = window.session
    assert project is not None
    template = load_template(resolve_active_template(project.project))  # type: ignore[arg-type]

    def build() -> ContinuousEngine:
        return ContinuousEngine(
            project.database, scan_session_id=session_id, template=template,
            recogniser=InlineRecogniser(template),
            intake_factory=lambda: IntakeService(project.database, project.root),
            limits=EngineLimits(max_in_flight=2, claim_window=2),
            unit_policy=UnitPolicy(max_unit_size=50, trickle_seconds=0),
        )

    return build


def test_a_real_kill_then_reopen_shows_committed_counts_before_start(qtbot, tmp_path):
    files = {
        "scanner-a": [(f"a{i}.png", SHEETS[i]) for i in range(6)],
        "scanner-b": [(f"b{i}.png", SHEETS[i + 6]) for i in range(4)],
    }
    root, session_id = prepare(tmp_path, "recover", files)
    log = tmp_path / "recover.jsonl"
    child = launch(
        root, log, "engine", session=session_id, expect=10, pause="in_commit",
        sheet=digest(SHEETS[3]),  # the stored copy's name is its content hash
        force_lock=False,
    )
    wait_for(child, paused, timeout=300, what="inside the commit of a3")
    assert kill(child).uncontained == ()
    done_before = committed(root)
    shape_before = structure(root)
    assert 0 < done_before < 10

    window = open_window(qtbot, tmp_path, root)
    scan = window._scan_page()
    assert scan is not None
    mode = scan.session_mode
    assert mode.active, "a session with sources reopens in session mode"
    assert not mode.running, "nothing starts by itself on open"
    view = mode.view
    assert view is not None
    # Reconstructed from committed rows, shown before anything is started.
    assert view.snapshot.recognition.done == done_before
    assert scan.session_panel.progress_labels["recognition"].text().startswith(
        f"{done_before:,} / "
    )
    assert "not running in this window" in scan.session_panel.state_line_label.text()
    # Work is outstanding, yet nothing reads it: the headline never implies motion.
    assert scan.session_panel.activity_label.text() == "Processing (not running in this window)"
    assert "Processing: running" in scan.session_panel.state_line_label.text()
    # The same session and batches: recovery creates neither.
    assert structure(root) == shape_before
    assert {item.label for item in view.sources} == {"scanner-a", "scanner-b"}
    # Resolve has its queue without visiting Scan.
    resolve = window._resolve_page()
    assert resolve is not None and resolve.state.scan_session_id == session_id

    mode.engine_factory = inline_factory(window, session_id)  # type: ignore[assignment]
    with qtbot.waitSignal(mode.runner.engine_started, timeout=60_000):
        assert mode.start()
    deadline = time.monotonic() + 300
    while committed(root) < 10 and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    assert committed(root) == 10
    window.close()
    QApplication.processEvents()
    assert not mode.running


def test_a_persisted_pause_is_shown_after_reopening(qtbot, tmp_path):
    root, session_id = prepare(tmp_path, "paused", {"scanner-a": [("0.png", SHEETS[0])]})
    from omr_scanner.services import open_project

    project = open_project(root)
    try:
        session_controls.pause_processing(project.database, session_id, actor="Operator")
        session_controls.set_intake_paused(project.database, session_id, True, actor="Operator")
    finally:
        project.close()
    window = open_window(qtbot, tmp_path, root)
    scan = window._scan_page()
    assert scan is not None
    panel = scan.session_panel
    assert scan.session_mode.view is not None
    assert scan.session_mode.view.snapshot.controls.processing is ProcessingIntent.PAUSED
    assert panel.activity_label.text() == "Processing paused"
    assert panel.processing_button.text() == "Resume Processing"
    assert panel.intake_button.text() == "Resume Intake"
    assert "Processing: paused" in panel.state_line_label.text()
    window.close()
