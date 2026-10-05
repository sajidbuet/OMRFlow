"""Native evidence for the two phase 8 pre-merge corrections.

1. The Answer Key stage names the scan session being worked through
   (open / closed / reopened), with the shared wording.
2. Resolve's provenance beside the evidence tabs stays readable with a long
   scanner name and a long original file name, never squeezing the tabs or
   widening the page.

The real main window on the native platform (this machine: 175 % Windows
scaling), at 1366x768 and 1100x680, interface zoom 100 / 150 / 200 %. The
session is the engine rig's (fake disk and clock, real recognition inline),
with a source named ``Station - Electrical Machines Laboratory North Wing``
and a sheet arriving as
``2026-10-05_Final_Examination_EEE_415_Section_A_Student_1000001_rescan_02.png``
that duplicates another sheet's Student ID.

Run from the repository root:

    python .claude/skills/qtguitesting/scripts/capture_phase8_corrections.py

Output: ``test-output/gui/phase8_corrections/`` - screenshots and
``report.json`` (git-ignored).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
for entry in (REPO / "src", REPO, HERE):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from capture_session_states import SIZES, clipped_controls, settle  # noqa: E402

OUT = REPO / "test-output" / "gui" / "phase8_corrections"
STATION = "Station - Electrical Machines Laboratory North Wing"
LONG_FILE = "2026-10-05_Final_Examination_EEE_415_Section_A_Student_1000001_rescan_02.png"
ZOOMS = (100, 150, 200)


def main() -> int:
    """Build the session, then capture each page at each size and zoom."""
    from tests.engine_rig import EngineRig, readable_sheets
    from tests.integration.test_session_finish import BIG, resolve_all_conflicts

    from omr_scanner.config import AppConfig
    from omr_scanner.gui.application import configure_application
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.services import (
        create_project,
        save_template,
        scan_sessions,
        session_finish,
        set_active_template,
    )

    app = QApplication(sys.argv)
    configure_application(app)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix="omr_phase8_corrections_"))
    project = create_project(work, "Final Exam 2026")
    rig = EngineRig(project)
    scan_sessions.rename_scan_session(
        project.database, rig.session_id, "EEE 415 Final - morning sitting",
        renamed_by="Dr. Operator",
    )
    rig.source(STATION)
    sheets = readable_sheets(20)
    rig.write(STATION, [(LONG_FILE, sheets[17]), ("short.png", sheets[18])])
    rig.new_engine(unit_policy=BIG)
    rig.make_ready()
    rig.run()
    template_file = project.project.layout.templates_dir / "rig.omrt"
    template_file.parent.mkdir(parents=True, exist_ok=True)
    save_template(rig.template, template_file)
    set_active_template(project, template_file)

    window = MainWindow(config=AppConfig(), config_path=work / "config.json")
    window.apply_reviewer_name("Dr. Operator")
    window._session = project
    window._broadcast_project_change()
    window.show()
    settle()
    report: dict[str, Any] = {"answer_key": [], "resolve": []}

    def answer_key_state(state: str) -> None:
        page = window._answer_key_page()
        for size, (width, height) in SIZES.items():
            for zoom in ZOOMS:
                window.set_interface_zoom(zoom)
                window.showNormal()
                window.resize(width, height)
                window.show_page("answer_key")
                settle()
                folder = OUT / size
                folder.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(folder / f"answer_key_{state}_{zoom:03d}.png"))
                label = page.session_label
                report["answer_key"].append({
                    "state": state, "size": size, "zoom": zoom,
                    "window": [window.width(), window.height()],
                    "text": label.text(), "visible": label.isVisible(),
                    "label_rect": [label.width(), label.height()],
                    "label_height_needed": label.heightForWidth(label.width()),
                    "stack_horizontal_scroll": window.stack.horizontalScrollBar().isVisible(),
                    "clipped": clipped_controls(page),
                })
                print(f"answer key {state} {size} {zoom}%: {label.text()!r}")
        window.set_interface_zoom(100)

    answer_key_state("open")

    # Resolve, on the long-named sheet.
    resolve = window._resolve_page()
    window.show_page("resolve")
    resolve.load_session(rig.session_id)
    deadline = time.monotonic() + 60
    while not resolve.state.conflicts and time.monotonic() < deadline:
        settle(2)
    for row, conflict in enumerate(resolve.state.conflicts):
        if conflict.scan_name == LONG_FILE:
            resolve.queue_table.selectRow(row)
            break
    deadline = time.monotonic() + 60
    while resolve.state.bundle is None and time.monotonic() < deadline:
        settle(2)
        time.sleep(0.02)
    label = resolve.provenance_context_label
    for size, (width, height) in SIZES.items():
        for zoom in ZOOMS:
            window.set_interface_zoom(zoom)
            window.showNormal()
            window.resize(width, height)
            window.show_page("resolve")
            settle()
            folder = OUT / size
            folder.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(folder / f"resolve_long_provenance_{zoom:03d}.png"))
            tabs = resolve.view_tabs
            bar = tabs.tabBar()
            corner_right = label.mapTo(window, label.rect().topRight()).x()
            report["resolve"].append({
                "size": size, "zoom": zoom, "window": [window.width(), window.height()],
                "decision_sheet_row": resolve.machine_summary_label.text(),
                "shown": label.text(), "full": label.full_text, "elided": label.is_elided,
                "label_width": label.width(),
                "text_fits_label": label.fontMetrics().horizontalAdvance(label.text())
                <= label.width(),
                "label_inside_window": 0 <= corner_right <= window.width(),
                "tab_bar_width": bar.width(), "tab_bar_needs": bar.sizeHint().width(),
                "tabs_whole": bar.width() >= bar.sizeHint().width(),
                "page_minimum_width": resolve.minimumSizeHint().width(),
                "stack_horizontal_scroll": window.stack.horizontalScrollBar().isVisible(),
                "tooltip": label.toolTip(),
            })
            print(f"resolve {size} {zoom}%: shown={label.text()!r} tabs_whole="
                  f"{bar.width() >= bar.sizeHint().width()}")
    window.set_interface_zoom(100)

    # Closed, then reopened: the Answer Key heading follows.
    resolve_all_conflicts(rig)
    outcome = rig.engine.finish_session(closed_by="Dr. Operator")  # type: ignore[union-attr]
    report["closed"] = bool(outcome.closed)
    window._on_active_session_changed()
    answer_key_state("closed")
    session_finish.reopen_session(project.database, rig.session_id, reopened_by="Dr. Operator")
    window._on_active_session_changed()
    answer_key_state("reopened")

    window._session = None
    window._broadcast_project_change()
    report["after_close_text"] = window._answer_key_page().session_label.text()
    window.close()
    project.close()
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
