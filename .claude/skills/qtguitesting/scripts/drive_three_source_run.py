"""Drive the real application through a three-scanner session (revised phase 8).

Purpose:
    Scripted local validation of the Scan stage's session mode on this
    machine with **production parts**: the real main window, the real
    worker-process recognition pool (``production_engine_factory``), the real
    disk and three real folders fed over time by
    ``scripts/dev_feed_scanner_folders.py`` in a separate process (partial
    writes, Scanner B unreachable for a while, damaged sheets whose rescans
    arrive at Scanner C).

    The script plays the operator through the controller's own entry points
    (the same methods the buttons call): add three sources, start, pause and
    resume processing, pause and resume intake, open Resolve, attempt *Finish
    scan session* (blockers expected), *Finish current and stop*, close the
    window. Throughout, a 50 ms timer measures how late the GUI event loop
    runs.

    **Not** an operator using the application by hand, not network shares,
    not real scanners - local, scripted evidence only.

Run from the repository root:

    python .claude/skills/qtguitesting/scripts/drive_three_source_run.py

Output: ``test-output/gui/three_source_run/`` (git-ignored): screenshots and
``report.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication

REPO = Path(__file__).resolve().parents[4]
for entry in (REPO / "src", REPO):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

OUT = REPO / "test-output" / "gui" / "three_source_run"
FEEDER = REPO / "scripts" / "dev_feed_scanner_folders.py"
OPERATOR = "Dr. Operator"


class LoopProbe:
    """How late a 50 ms timer fires - the event loop's responsiveness."""

    def __init__(self) -> None:
        self.lateness_ms: list[float] = []
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last = 0
        self._timer = QTimer()
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        now = self._clock.elapsed()
        if self._last:
            self.lateness_ms.append(max(0.0, float(now - self._last - 50)))
        self._last = now

    def summary(self) -> dict[str, float]:
        """Median, 95th percentile and worst lateness, in milliseconds."""
        values = sorted(self.lateness_ms) or [0.0]
        return {
            "samples": len(values),
            "median_ms": statistics.median(values),
            "p95_ms": values[int(len(values) * 0.95) - 1] if len(values) > 1 else values[0],
            "worst_ms": values[-1],
        }


def pump(seconds: float) -> None:
    """Run the event loop for ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def until(predicate: Callable[[], bool], timeout: float, what: str) -> bool:
    """Run the event loop until ``predicate`` or ``timeout``; say which."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            print(f"  (not reached within {timeout:.0f} s: {what})")
            return False
        pump(0.1)
    return True


def main() -> int:
    """Play the scenario; write the screenshots and the report."""
    from omr_scanner.config import AppConfig
    from omr_scanner.domain.session_snapshot import SessionActivity
    from omr_scanner.gui import session_close
    from omr_scanner.gui.application import configure_application
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.gui.scan.session_dialogs import SourceDraft

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="omr_three_source_"))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]))
    prepared = subprocess.run(
        [sys.executable, str(FEEDER), "prepare", "--root", str(work)],
        check=True, capture_output=True, text=True, env=env,
    ).stdout
    project_root = Path(re.search(r"Project: (.+)", prepared).group(1).strip())  # type: ignore[union-attr]
    folders = dict(re.findall(r"(Scanner [ABC]): (.+)", prepared))

    app = QApplication(sys.argv)
    configure_application(app)
    window = MainWindow(config=AppConfig(), config_path=work / "config.json")
    window.apply_reviewer_name(OPERATOR)
    window.resize(1366, 768)
    window.show()
    pump(0.5)
    report: dict[str, Any] = {"steps": [], "samples": []}

    def step(name: str, **detail: Any) -> None:
        detail["t"] = round(time.monotonic() - started, 1)
        report["steps"].append({"step": name, **detail})
        print(f"[{detail['t']:6.1f}] {name} {detail if len(detail) > 1 else ''}")

    def shot(name: str, page: str = "scan") -> None:
        window.show_page(page)
        pump(0.6)
        window.grab().save(str(OUT / f"{name}.png"))

    started = time.monotonic()
    assert window.open_project_resolving_lock(project_root, action="force")
    pump(0.5)
    scan = window._scan_page()
    assert scan is not None
    mode = scan.session_mode
    for label in ("Scanner A", "Scanner B", "Scanner C"):
        mode.add_source(SourceDraft(label=label, root_path=folders[label].strip()))
    pump(0.5)
    step("three sources added", active=mode.active, session=mode.scan_session_id)
    probe = LoopProbe()
    if not mode.start():
        templates = sorted((project_root / "templates").glob("*.omrt"))
        scan.load_template_from(templates[0])
        assert mode.start(), "start refused"
    until(lambda: mode.running, 120, "engine started")
    step("continuous scanning started (worker-process pool)")
    shot("01_started")

    feeder = subprocess.Popen(
        [sys.executable, str(FEEDER), "feed", "--root", str(work), "--sheets", "90",
         "--rate", "3", "--outage", "15", "--damage", "3", "--partial-every", "10"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    seen_activities: set[str] = set()
    unreachable_text = ""
    last_sample = 0.0
    paused_processing = paused_intake = False
    phase_started = time.monotonic()

    def sample() -> None:
        nonlocal unreachable_text
        view = mode.view
        if view is None:
            return
        panel = scan.session_panel
        seen_activities.add(panel.activity_label.text())
        if view.snapshot.activity is SessionActivity.WAITING_FOR_SOURCE and not unreachable_text:
            unreachable_text = panel.activity_label.text()
            shot("03_scanner_b_unreachable")
            window.show_page("scan")
        report["samples"].append({
            "t": round(time.monotonic() - started, 1),
            "activity": panel.activity_label.text(),
            "recognition": panel.progress_labels["recognition"].text(),
            "conflicts": panel.progress_labels["conflicts"].text(),
            "rescans": panel.progress_labels["rescans"].text(),
            "sources": [(item.label, panel_status) for item, panel_status in zip(
                view.sources,
                [panel.sources_table.item(row, 1).text()
                 for row in range(panel.sources_table.rowCount())], strict=False)],
        })

    while feeder.poll() is None:
        pump(0.2)
        now = time.monotonic()
        if now - last_sample >= 2.0:
            sample()
            last_sample = now
        elapsed = now - phase_started
        if elapsed > 10 and not paused_processing:
            paused_processing = True
            mode.pause_processing()
            step("Pause Processing")
            pump(3)
            shot("02_processing_paused")
            window.show_page("scan")
            pump(2)
            mode.resume_processing()
            step("Resume Processing")
        if elapsed > 22 and not paused_intake:
            paused_intake = True
            mode.pause_intake()
            step("Pause Intake")
            pump(4)
            mode.resume_intake()
            step("Resume Intake")
    step("feeder finished", output=(feeder.stdout.read() if feeder.stdout else "")[-600:])

    def caught_up() -> bool:
        view = mode.view
        return view is not None and view.snapshot.activity is SessionActivity.CAUGHT_UP

    until(caught_up, 600, "caught up")
    sample()
    step("caught up", activity=scan.session_panel.activity_label.text(),
         recognition=scan.session_panel.progress_labels["recognition"].text())
    shot("04_caught_up")

    resolve = window._resolve_page()
    assert resolve is not None
    window.show_page("resolve")
    until(lambda: resolve.state.scan_session_id == mode.scan_session_id, 60, "Resolve loaded")
    pump(3)
    names = [resolve.queue_table.item(row, 0).text()
             for row in range(resolve.queue_table.rowCount())
             if resolve.queue_table.item(row, 0) is not None]
    step("Resolve conflicts", rows=len(names),
         hash_named=sum(bool(re.search(r"[0-9a-f]{32,}", name)) for name in names),
         summary=resolve.summary_label.text())
    shot("05_resolve_conflicts", "resolve")
    resolve.show_view("suggestions")
    pump(2)
    step("Resolve suggested rescans", rows=len(resolve.state.suggestions))
    shot("06_resolve_suggestions", "resolve")

    window.show_page("scan")
    blockers = session_close.preview_blockers(window.session, mode.scan_session_id)
    step("finish preview", blockers=[str(getattr(item, "code", item)) for item in blockers])
    outcomes: list[object] = []
    mode.finish_completed.connect(outcomes.append)
    mode.finish_session()
    until(lambda: bool(outcomes), 300, "finish outcome")
    outcome = outcomes[-1] if outcomes else None
    step("Finish Scan Session attempted", closed=bool(getattr(outcome, "closed", False)),
         codes=[str(code) for code in getattr(outcome, "codes", ())])

    mode.finish_current()
    step("Finish Current and Stop")
    until(lambda: not mode.running, 300, "engine stopped")
    step("engine stopped", running=mode.running)
    shot("07_stopped_open")
    report["event_loop"] = probe.summary()
    report["activities_seen"] = sorted(seen_activities)
    report["unreachable_wording"] = unreachable_text
    window.close()
    pump(1)
    step("window closed")
    try:
        import psutil

        children = psutil.Process().children(recursive=True)
        report["child_processes_after_close"] = [child.name() for child in children]
    except ImportError:  # pragma: no cover - optional
        report["child_processes_after_close"] = "psutil not installed"
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "event_loop", "activities_seen", "unreachable_wording", "child_processes_after_close"
    )}, indent=2, default=str))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
