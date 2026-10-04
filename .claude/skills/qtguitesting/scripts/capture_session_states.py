"""Capture the operational scan-session GUI in every significant state (revised phase 8).

Purpose:
    Evidence for ``PHASE_H_HANDOFF.md`` §16 / ACCEPTANCE_CRITERIA F7: the real
    main window, on the native platform (real font metrics, the machine's own
    display scaling), in each state the phase adds - finite mode unchanged,
    session idle, processing, caught up, paused, intake paused, an unreachable
    scanner, the registration-failure warning, conflicts arriving in Resolve,
    suggested rescans, a replacement candidate, a file awaiting a decision,
    the finish-session blockers, closed, reopened (stale outputs) and a
    session reopened after an interruption, before Start - at 1366x768 (dense
    states also at 1100x680), plus the session panel, Resolve and the finish
    dialog at 80/100/120/150/200 % interface zoom.

    Next to each screenshot a JSON report records the geometry a picture
    cannot prove: controls narrower than their own minimum (clipped), primary
    actions outside the window, dialogs larger than the screen, and the
    sheet canvas zoom surviving an interface-zoom change.

The session runs on the test suite's engine rig (a fake disk and clock, the
stress dataset's real synthetic sheets, real recognition inline) so every
state is reached deterministically. Nothing here exists in the application;
the two injection points used - the engine factory and the snapshot clock -
are the session-mode controller's own, and ``session_close.make_intake`` is
pointed at the rig's disk for the duration of the script.

Run from the repository root, on the native platform:

    python .claude/skills/qtguitesting/scripts/capture_session_states.py

Output: ``test-output/gui/session_states/`` (git-ignored).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

REPO = Path(__file__).resolve().parents[4]
for entry in (REPO / "src", REPO):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

OUT = REPO / "test-output" / "gui" / "session_states"
SIZES = {"1366x768": (1366, 768), "1100x680": (1100, 680)}
ZOOMS = [80, 100, 120, 150, 200]
SAMPLE_SHEET = REPO / "examples" / "ECE-0000.png"
SAMPLE_TEMPLATE = REPO / "examples" / "templates" / "ece_0000_sample.omrt"


def settle(rounds: int = 8) -> None:
    """Let Qt run its pending layout and paint events."""
    app = QApplication.instance()
    for _ in range(rounds):
        app.processEvents()  # type: ignore[union-attr]


def clipped_controls(root: QWidget) -> list[str]:
    """Visible buttons / single-line labels with less room than their own minimum."""
    problems = []
    for widget in root.findChildren(QWidget):
        if not widget.isVisible() or not isinstance(widget, (QAbstractButton, QLabel)):
            continue
        if isinstance(widget, QLabel) and (widget.wordWrap() or not widget.text()):
            continue
        hint = widget.minimumSizeHint()
        if hint.width() <= 0:
            continue
        if widget.width() + 1 < hint.width() or widget.height() + 1 < hint.height():
            name = widget.objectName() or getattr(widget, "text", lambda: "")()
            problems.append(
                f"{type(widget).__name__}:{name!r} {widget.width()}x{widget.height()} < "
                f"{hint.width()}x{hint.height()}"
            )
    return problems


def outside(window: QWidget, widgets: list[QWidget]) -> list[str]:
    """Visible widgets whose rectangle leaves the window (not reachable without scrolling)."""
    found = []
    frame = window.rect()
    for widget in widgets:
        if not widget.isVisible():
            continue
        top_left = widget.mapTo(window, widget.rect().topLeft())
        bottom_right = widget.mapTo(window, widget.rect().bottomRight())
        if not (frame.contains(top_left) and frame.contains(bottom_right)):
            found.append(widget.objectName() or type(widget).__name__)
    return found


class Capture:
    """Screenshots and geometry records, by state and size."""

    def __init__(self, window: Any) -> None:
        self.window = window
        self.report: dict[str, Any] = {"states": [], "zoom": []}

    def state(self, name: str, page: str, *, sizes: tuple[str, ...] = ("1366x768",),
              extra: Callable[[], dict[str, Any]] | None = None) -> None:
        """Screenshot ``page`` in the window at each size, with its geometry record."""
        window = self.window
        for size in sizes:
            width, height = SIZES[size]
            window.showNormal()
            window.resize(width, height)
            window.show_page(page)
            settle()
            folder = OUT / size
            folder.mkdir(parents=True, exist_ok=True)
            target = window._pages[page]
            window.grab().save(str(folder / f"{name}.png"))
            scan = window._scan_page()
            panel = scan.session_panel
            primary = [
                panel.start_button, panel.processing_button, panel.stop_button,
                panel.intake_button, panel.add_source_button, panel.finish_session_button,
                panel.reopen_session_button,
            ] if page == "scan" else []
            record = {
                "state": name,
                "size": size,
                "window": [window.width(), window.height()],
                "page": page,
                "clipped": clipped_controls(target),
                "primary_actions_outside_window": outside(window, primary),
                "stack_scrollbars": [
                    window.stack.horizontalScrollBar().isVisible(),
                    window.stack.verticalScrollBar().isVisible(),
                ],
            }
            if extra is not None:
                record.update(extra())
            self.report["states"].append(record)
            print(f"{size} {name}: clipped={len(record['clipped'])} "
                  f"outside={record['primary_actions_outside_window']}")

    def dialog(self, name: str, dialog: QWidget, size: str = "1366x768") -> None:
        """Screenshot a dialog and record whether it fits the screen."""
        dialog.show()
        settle()
        folder = OUT / size
        folder.mkdir(parents=True, exist_ok=True)
        dialog.grab().save(str(folder / f"{name}.png"))
        screen = dialog.screen().availableGeometry()
        self.report["states"].append({
            "state": name,
            "size": size,
            "dialog": [dialog.width(), dialog.height()],
            "screen": [screen.width(), screen.height()],
            "inside_screen": dialog.width() <= screen.width()
            and dialog.height() <= screen.height(),
            "clipped": clipped_controls(dialog),
        })
        dialog.close()


def wait(predicate: Callable[[], bool], rig: Any, *, timeout: float = 180.0,
         tick: float = 0.0) -> None:
    """Pump events (refreshing the snapshot, advancing the fake clock) until ``predicate``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("state not reached")
        if tick:
            rig.clock.advance(tick)
        rig.page.session_mode.poller.refresh()
        settle(4)
        time.sleep(0.03)


def main() -> int:
    """Walk through every state and write the screenshots and the report."""
    from omr_scanner.config import AppConfig
    from omr_scanner.domain.session_snapshot import SessionActivity
    from omr_scanner.gui import session_close
    from omr_scanner.gui.application import configure_application
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.gui.scan.session_dialogs import FinishSessionDialog
    from omr_scanner.services import quality_decisions, scan_sessions
    from omr_scanner.services.intake import IntakeService

    app = QApplication(sys.argv)
    configure_application(app)
    work = Path(tempfile.mkdtemp(prefix="omr_session_states_"))
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    from tests.engine_rig import KillAt, Killed, readable_sheets, sheet_bytes
    from tests.gui.session_gui_rig import SessionGuiRig
    from tests.integration.test_session_finish import resolve_all_conflicts
    from tests.quality_rig import blank_page, displaced_id, rescan_of

    window = MainWindow(config=AppConfig(), config_path=work / "config.json")
    window.apply_reviewer_name("Dr. Operator")
    window.show()
    settle()
    capture = Capture(window)

    # -- 1. finite mode, unchanged ----------------------------------------
    finite_root = work / "finite"
    finite_root.mkdir()
    window.create_project_at(finite_root, "Finite Exam")
    scan = window._scan_page()
    if SAMPLE_TEMPLATE.is_file() and SAMPLE_SHEET.is_file():
        scan.load_template_from(SAMPLE_TEMPLATE)
        scan.add_scan_paths([SAMPLE_SHEET])
        done: list[object] = []
        scan.batch_finished.connect(done.append)
        scan.process_all()
        deadline = time.monotonic() + 180
        while not done and time.monotonic() < deadline:
            settle(2)
            time.sleep(0.05)
    capture.state("01_finite_scan_unchanged", "scan", sizes=("1366x768", "1100x680"),
                  extra=lambda: {"session_mode": scan.session_mode.active})
    window.close_project()

    # -- the continuous session ------------------------------------------
    session_root = work / "session"
    session_root.mkdir()
    window.create_project_at(session_root, "Final Exam 2026")
    project = window.session
    rig = SessionGuiRig(project)
    scan_sessions.rename_scan_session(
        project.database, rig.session_id, "Final Exam 2026 - morning sitting",
        renamed_by="Dr. Operator",
    )
    session_close.make_intake = lambda project_: IntakeService(  # type: ignore[assignment]
        project_.database, project_.root, fs=rig.fs, clock=rig.clock, recover=False
    )
    for name in ("A", "B", "C"):
        rig.source(name)
    sheets = readable_sheets(60)
    rig.write("A", [(f"A-{i:03d}.png", sheets[i]) for i in range(0, 14)])
    rig.write("B", [(f"B-{i:03d}.png", sheets[i]) for i in range(14, 26)]
              + [("B-moved.png", displaced_id(3))])
    rig.write("C", [(f"C-{i:03d}.png", sheets[i]) for i in range(26, 38)]
              + [("C-broken.png", sheet_bytes(36)[30])])
    scan.state.template = rig.template
    mode = scan.session_mode
    mode.engine_factory = rig.factory()
    mode.poller.clock = rig.clock
    rig.page = scan
    window._broadcast_project_change()
    settle()

    capture.state("02_session_idle_open", "scan")
    mode.start()
    wait(lambda: mode.running and mode.view is not None
         and mode.view.snapshot.recognition.done >= 6, rig, tick=3)
    capture.state("03_processing", "scan", sizes=("1366x768", "1100x680"))
    wait(lambda: mode.view is not None
         and mode.view.snapshot.activity is SessionActivity.CAUGHT_UP, rig, tick=3)
    capture.state("04_caught_up", "scan")
    mode.pause_processing()
    wait(lambda: mode.view.snapshot.activity is SessionActivity.PROCESSING_PAUSED, rig)
    capture.state("05_processing_paused", "scan")
    mode.resume_processing()
    mode.pause_intake()
    wait(lambda: mode.view.snapshot.activity is SessionActivity.INTAKE_PAUSED, rig, tick=1)
    capture.state("06_intake_paused", "scan")
    mode.resume_intake()
    scan.session_panel.sources_section.set_expanded(True)
    rig.fs.unreachable.add(rig.root("C"))
    wait(lambda: mode.view.snapshot.activity is SessionActivity.WAITING_FOR_SOURCE, rig, tick=1)
    capture.state("07_source_unreachable", "scan", sizes=("1366x768", "1100x680"))
    rig.fs.unreachable.discard(rig.root("C"))
    # Registration failures at one scanner: blank pages.
    mode.pause_intake()
    rig.write("B", [(f"B-blank-{i}.png", blank_page(i)) for i in range(6)])
    mode.resume_intake()
    wait(lambda: any(item.alarm is not None and item.alarm.raised
                     for item in mode.view.snapshot.sources), rig, tick=3)
    capture.state("08_registration_alarm", "scan")
    scan.session_panel.sources_section.set_expanded(False)

    # -- Resolve while scanning ---------------------------------------------
    resolve = window._resolve_page()
    window.show_page("resolve")
    mode.pause_intake()
    rig.write("A", [("A-late-dup.png", sheets[18])])  # a Student ID already read at B
    mode.resume_intake()
    wait(lambda: resolve.state.scan_session_id == rig.session_id
         and bool(resolve.state.conflicts), rig, tick=3)
    capture.state("09_resolve_conflicts_live", "resolve", sizes=("1366x768", "1100x680"))
    resolve.show_view("suggestions")
    settle()
    capture.state("10_rescan_suggestions", "resolve", sizes=("1366x768", "1100x680"))
    suggestions = quality_decisions.outstanding_suggestions(project.database, rig.session_id)
    moved = next((item for item in suggestions if item.source_name), None)
    if moved is not None:
        quality_decisions.confirm_suggestion(project.database, moved.scan_id,
                                             reviewer="Dr. Operator")
        mode.pause_intake()
        rig.write("C", [("C-rescan-of-moved.png", rescan_of(3))])
        mode.resume_intake()
        wait(lambda: mode.view.snapshot.partition.queued == 0
             and mode.view.snapshot.partition.ready == 0, rig, tick=3)
        resolve.show_view("rescans")
        settle()
        resolve.select_case(moved.scan_id)
        settle()
        if resolve.rescan_panel.candidates_list.count():
            resolve.rescan_panel.candidates_list.setCurrentRow(0)
        capture.state("11_replacement_candidate", "resolve", sizes=("1366x768", "1100x680"))
    for _ in range(5):
        rig.clock.advance(60)
        settle(2)
    resolve.show_view("files")

    def file_waiting() -> bool:
        resolve.refresh_queue()
        return bool(resolve.state.pending_files)

    wait(file_waiting, rig, tick=30, timeout=120)
    capture.state("12_file_awaiting_decision", "resolve")

    # -- Finish: blockers, closed, reopened -----------------------------------
    window.show_page("scan")
    blockers = session_close.preview_blockers(project, rig.session_id)
    dialog = FinishSessionDialog("Final Exam 2026 - morning sitting", blockers,
                                 attempted=False, operator="Dr. Operator", parent=window)
    capture.dialog("13_finish_blockers_preview", dialog)
    for size in ("1100x680",):
        window.resize(*SIZES[size])
        settle()
        again = FinishSessionDialog("Final Exam 2026 - morning sitting", blockers,
                                    attempted=True, operator="Dr. Operator", parent=window)
        capture.dialog("13_finish_blockers_after_attempt", again, size)
    # Clear what blocks: answer the suggestions and files, resolve conflicts.
    for item in quality_decisions.outstanding_suggestions(project.database, rig.session_id):
        quality_decisions.dismiss_suggestion(project.database, item.scan_id,
                                             reviewer="Dr. Operator")
    from omr_scanner.services import intake_decisions

    for item in intake_decisions.pending_decisions(project.database, rig.session_id):
        intake_decisions.decide_file(project.database, item.intake_file_id,
                                     intake_decisions.FileDecision.DISMISS,
                                     reviewer="Dr. Operator")
    resolve_all_conflicts(rig)
    outcomes: list[object] = []
    mode.finish_completed.connect(outcomes.append)
    mode.finish_session(accept_incomplete=True)
    wait(lambda: bool(outcomes), rig, timeout=120)
    wait(lambda: mode.view.snapshot.session_state == "closed", rig)
    capture.state("14_closed", "scan", extra=lambda: {"finish": repr(outcomes[-1])[:300]})
    mode.reopen_session(reason="late script found")
    wait(lambda: mode.view.snapshot.session_state == "open", rig)
    capture.state("15_reopened_stale", "scan")
    capture.state("15_reopened_stale_results", "results")
    capture.state("15_reopened_stale_reports", "reports")
    scan.shutdown_background_work()

    # -- interrupted, then reopened before Start ---------------------------------
    rig.write("A", [(f"A-after-{i}.png", sheets[40 + i]) for i in range(8)])
    rig.new_engine(hooks=KillAt("committed", 3))
    rig.make_ready()
    try:
        rig.run()
    except Killed:
        rig.engine = None
    root = project.root
    window.close_project()
    window.open_project_resolving_lock(root, action="force")
    scan = window._scan_page()
    scan.session_mode.poller.clock = rig.clock
    scan.session_mode.refresh_mode()
    settle()
    capture.state("16_recovered_before_start", "scan", sizes=("1366x768", "1100x680"))

    # -- the interface-zoom matrix -------------------------------------------
    for size, (width, height) in SIZES.items():
        window.showNormal()
        window.resize(width, height)
        for percent in ZOOMS if size == "1366x768" else [100, 200]:
            window.set_interface_zoom(percent)
            settle()
            folder = OUT / "zoom" / size
            folder.mkdir(parents=True, exist_ok=True)
            for page in ("scan", "resolve"):
                window.show_page(page)
                settle()
                window.grab().save(str(folder / f"{percent:03d}_{page}.png"))
                target = window._pages[page]
                scan_panel = window._scan_page().session_panel
                capture.report["zoom"].append({
                    "size": size, "zoom": percent, "page": page,
                    "clipped": clipped_controls(target),
                    "primary_actions_outside_window": outside(window, [
                        scan_panel.start_button, scan_panel.processing_button,
                        scan_panel.finish_session_button, scan_panel.intake_button,
                    ]) if page == "scan" else [],
                    "stack_scrollbars": [
                        window.stack.horizontalScrollBar().isVisible(),
                        window.stack.verticalScrollBar().isVisible(),
                    ],
                })
            blockers = session_close.preview_blockers(window.session, rig.session_id)
            dialog = FinishSessionDialog("Final Exam 2026", blockers, attempted=True,
                                         operator="Dr. Operator", parent=window)
            capture.dialog(f"zoom/{size}/{percent:03d}_finish_dialog", dialog, ".")
    window.set_interface_zoom(100)
    window.close_project()
    window.close()
    (OUT / "report.json").write_text(json.dumps(capture.report, indent=2, default=str),
                                     encoding="utf-8")
    print(f"Wrote {OUT}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
