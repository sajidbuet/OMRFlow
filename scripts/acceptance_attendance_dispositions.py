"""Rendered acceptance run: Attendance dispositions in the real OMRFlow window.

Scenario (every identifier fictional):
    * candidate 10000049 scanned twice (the same sheet, two files),
    * a stray sheet read as 10000009, who is on no candidate list,
    * candidate 10000051 whose sheet the operator is unsure about (deferred),
    * several ordinary, valid scripts.

Sheets are rendered and read through the real recognition pipeline; the
project is a real one on disk; the window is the real ``MainWindow`` on the
platform's own display (not ``offscreen`` unless forced). The two modal
questions are real dialogs, answered by a timer that screenshots and clicks
them. Screenshots at 1366x768 and 1100x680 go to
``test-output/gui/attendance_dispositions/``; a JSON summary of every check
is written beside them. The script exits non-zero if any check fails.

Run from the repository root::

    .venv/Scripts/python scripts/acceptance_attendance_dispositions.py
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

OUT = REPOSITORY_ROOT / "test-output" / "gui" / "attendance_dispositions"
OPERATOR = "Dr. Acceptance Operator"
SIZES = ((1366, 768), (1100, 680))

checks: list[dict[str, object]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one acceptance check."""
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))


def main() -> int:  # noqa: D103 - the scenario is the module docstring
    import cv2
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from tests.conftest import build_answer_sheet_template, render_marked_sheet

    from omr_scanner.config import AppConfig
    from omr_scanner.domain.reconciliation import ReconciliationStatus
    from omr_scanner.domain.scan_lifecycle import LifecycleState
    from omr_scanner.gui.attendance.page import ExclusionDialog
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.services import (
        batch_store,
        create_project,
        reconciliation_store,
        review_store,
        save_template,
        scan_lifecycle,
        scoring_store,
        set_active_template,
    )
    from omr_scanner.services.answer_key import plan_for, read_key
    from omr_scanner.services.batch_processor import process_batch

    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    template = build_answer_sheet_template(roll_digits=8)
    plan = plan_for(template)

    workspace = REPOSITORY_ROOT / "test-output" / "projects" / uuid.uuid4().hex[:8]
    workspace.mkdir(parents=True)
    session = create_project(workspace, "Attendance Dispositions Acceptance")
    root = session.root
    written = save_template(template, session.project.layout.templates_dir / "sheet.omrt")
    set_active_template(session, written)

    def marks(roll: str, answer: str) -> dict:
        return {
            "roll_number": dict(enumerate(roll)),
            "set_code": {0: "A"},
            "questions_0": dict.fromkeys(range(10), answer),
            "questions_1": dict.fromkeys(range(10), answer),
        }

    sheets = [
        ("IMG_0001.png", "10000047", "A"),
        ("IMG_0002.png", "10000048", "A"),
        ("IMG_0003.png", "10000049", "A"),  # the same script ...
        ("IMG_0004.png", "10000049", "A"),  # ... scanned twice
        ("IMG_0005.png", "10000050", "B"),
        ("IMG_0006.png", "10000009", "A"),  # an unwanted sheet
        ("IMG_0007.png", "10000051", "A"),  # deferred below
    ]
    scans = workspace / "incoming"
    scans.mkdir()
    paths = []
    for name, roll, answer in sheets:
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks(roll, answer)))
        paths.append(path)
    roster = workspace / "candidates.csv"
    roster.write_text(
        "Sl.No.,Roll No.,Name,Total,Merit\n"
        "1,10000047,CANDIDATE P,,\n2,10000048,CANDIDATE Q,,\n"
        "3,10000049,CANDIDATE R,,\n4,10000050,CANDIDATE S,,\n"
        "5,10000051,CANDIDATE T,,\n",
        encoding="utf-8",
    )
    database = session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)
    ids = {path.name: scan_id for path, scan_id in batch_store.scan_ids_by_path(
        database, batch_id).items()}
    for item in report.processed:
        review_store.sync_conflicts(
            database, batch_id=batch_id, scan_id=ids[item.source_path.name],
            result=item.result, template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)
    key = read_key("A" * plan.question_count, plan, "A").to_key()
    stored = scoring_store.save_key(database, key)
    scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
    read = {row.filename: row.identifier_value for row in _scans(database, batch_id)}
    check("sheets read as rendered", read.get("IMG_0004.png") == "10000049"
          and read.get("IMG_0006.png") == "10000009", json.dumps(read))
    session.close()

    def pump(ms: int = 150) -> None:
        loop_end = QTimer()
        loop_end.setSingleShot(True)
        loop_end.start(ms)
        while loop_end.isActive():
            app.processEvents()

    def open_window() -> MainWindow:
        window = MainWindow(AppConfig(reviewer_name=OPERATOR),
                            config_path=workspace / "config.json")
        window.show()
        assert window.open_project_at(root)
        pump(300)
        return window

    def shoot(window: MainWindow, name: str) -> None:
        for width, height in SIZES:
            window.resize(width, height)
            pump(250)
            window.grab().save(str(OUT / f"{name}_{width}x{height}.png"))
        window.resize(*SIZES[0])
        pump(150)

    def answer_modal(save_as: str, accept_text: str) -> None:
        """When the next modal appears: screenshot it, then press ``accept_text``."""

        def act() -> None:
            modal = QApplication.activeModalWidget()
            if modal is None:
                QTimer.singleShot(100, act)
                return
            modal.grab().save(str(OUT / f"{save_as}.png"))
            if isinstance(modal, QMessageBox):
                for button in modal.buttons():
                    if button.text() == accept_text:
                        button.click()
                        return
            if isinstance(modal, ExclusionDialog):
                modal.note_edit.setText("Physics paper scanned by mistake")
                modal.confirm_button.click()
                return
            if isinstance(modal, QDialog):
                modal.reject()

        QTimer.singleShot(200, act)

    # ------------------------------------------------------------ first session
    window = open_window()
    window.show_page("attendance")
    page = window._attendance_page()
    assert page is not None
    page.set_batch(batch_id)
    page.import_from  # noqa: B018 - the import dialog is exercised elsewhere
    from omr_scanner.services.candidate_import import read_roster

    done = []
    page.reconciled.connect(lambda: done.append(1))
    page.commit_roster(read_roster(roster), source_path=roster)
    while not done:
        pump(100)
    page.status_filter.setCurrentIndex(1)  # exceptions only
    pump()
    shoot(window, "01_initial_exceptions")
    check("duplicate chip shows 1", page._chips["duplicate"].text().endswith(" 1"),
          page._chips["duplicate"].text())
    check("unrecognised chip shows 1", page._chips["unrecognised"].text().endswith(" 1"),
          page._chips["unrecognised"].text())

    # Duplicate: both copies inspectable.
    page.filter_by_chip("duplicate")
    pump()
    row = next(i for i, e in enumerate(page.state.entries) if e.candidate_id == "10000049")
    page.table.selectRow(row)
    pump(1500)
    check("duplicate exposes 2 scripts", page.scripts_list.count() == 2)
    check("navigator says Script 1 of 2", "Script 1 of 2" in page.script_position_label.text())
    first_preview = page.inspector.scan_id
    shoot(window, "02_duplicate_script1")
    page.step_script(1)
    pump(1500)
    check("second copy shown", page.inspector.scan_id not in (None, first_preview),
          f"{first_preview} -> {page.inspector.scan_id}")
    shoot(window, "03_duplicate_script2")

    # Keep the first copy.
    page.step_script(-1)
    pump(800)
    answer_modal("04_keep_confirmation", "Keep And Reject Others")
    kept = page.keep_selected_script()
    pump(500)
    check("keep recorded", kept)
    check("IMG_0004 excluded", scan_lifecycle.state_of(page.database, ids["IMG_0004.png"])
          is LifecycleState.EXCLUDED)
    page.status_filter.setCurrentIndex(0)
    pump()
    entry = next(e for e in page.state.entries if e.candidate_id == "10000049")
    check("10000049 now matched", entry.status is ReconciliationStatus.MATCHED,
          entry.status.value)
    shoot(window, "05_after_keep_all")

    # Unwanted sheet.
    page.filter_by_chip("unrecognised")
    pump()
    row = next(i for i, e in enumerate(page.state.entries) if e.candidate_id == "10000009")
    page.table.selectRow(row)
    pump(1500)
    shoot(window, "06_unknown_selected")
    answer_modal("07_exclusion_dialog", "")
    excluded = page.exclude_selected_script()
    pump(500)
    check("unwanted sheet excluded", excluded and scan_lifecycle.state_of(
        page.database, ids["IMG_0006.png"]) is LifecycleState.EXCLUDED)
    check("rejected chip shows 2", page._chips["rejected"].text().endswith(" 2"),
          page._chips["rejected"].text())
    page.filter_by_chip("rejected")
    pump(1500)
    page.table.selectRow(0)
    pump(1500)
    check("rejected view lists both sheets", len(page.state.dispositions) == 2)
    shoot(window, "08_rejected_view")

    # Deferred.
    page.status_filter.setCurrentIndex(0)
    pump()
    row = next(i for i, e in enumerate(page.state.entries) if e.candidate_id == "10000051")
    page.table.selectRow(row)
    pump(800)
    page.defer_selected_script()
    pump(500)
    entry = next(e for e in page.state.entries if e.candidate_id == "10000051")
    check("10000051 reads Script deferred", entry.status is ReconciliationStatus.SCRIPT_DEFERRED)
    page.status_filter.setCurrentIndex(1)
    pump()
    summary = page.summary_label.text()
    check("attendance warns about deferred", "deferred and will not be included" in summary)
    shoot(window, "09_after_defer_exceptions")

    # Results.
    results = window._results_page()
    assert results is not None
    window.show_page("results")
    results.set_batch(batch_id)
    scored = []
    results.scored.connect(lambda: scored.append(1))
    results.score_batch()
    for _ in range(200):
        if scored:
            break
        pump(100)
    pump(500)
    shown = {item.candidate_id: item for item in results.state.results}
    check("results computed", bool(scored))
    check("10000049 scored from the kept copy", shown.get("10000049") is not None
          and shown["10000049"].scan_id == ids["IMG_0003.png"])
    check("10000009 absent from results", "10000009" not in shown)
    check("results warn about deferred", "deferred and will not be included"
          in results.summary_label.text())
    shoot(window, "10_results_with_deferred")
    window.close()
    pump(300)

    # ------------------------------------------------------------ reopen
    window = open_window()
    window.show_page("attendance")
    page = window._attendance_page()
    assert page is not None
    page.set_batch(batch_id)
    pump(500)
    db = page.database
    check("after reopen: IMG_0004 still excluded",
          scan_lifecycle.state_of(db, ids["IMG_0004.png"]) is LifecycleState.EXCLUDED)
    check("after reopen: IMG_0006 still excluded",
          scan_lifecycle.state_of(db, ids["IMG_0006.png"]) is LifecycleState.EXCLUDED)
    check("after reopen: IMG_0007 still deferred",
          scan_lifecycle.state_of(db, ids["IMG_0007.png"]) is LifecycleState.DEFERRED)
    page.status_filter.setCurrentIndex(0)
    pump()
    entry = next(e for e in page.state.entries if e.candidate_id == "10000049")
    check("after reopen: 10000049 matched", entry.status is ReconciliationStatus.MATCHED)
    shoot(window, "11_reopened")

    # Restore the deferred sheet.
    page.filter_by_chip("deferred")
    pump(1500)
    page.table.selectRow(0)
    pump(1500)
    shoot(window, "12_deferred_view")
    page.restore_selected_script()
    pump(500)
    page.status_filter.setCurrentIndex(0)
    pump()
    entry = next(e for e in page.state.entries if e.candidate_id == "10000051")
    check("restored sheet back in review", entry.status is ReconciliationStatus.MATCHED
          and scan_lifecycle.state_of(db, ids["IMG_0007.png"]) is LifecycleState.ACTIVE,
          entry.status.value)
    roster_id = page.state.roster.roster_id if page.state.roster else 0
    counts = reconciliation_store.stored_counts(db, roster_id, batch_id)
    check("no duplicate after restore of the deferred sheet",
          counts is not None and counts.duplicate_script == 0)
    shoot(window, "13_after_restore")
    window.close()
    pump(300)

    (OUT / "summary.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    failed = [item for item in checks if not item["ok"]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed; screenshots in {OUT}")
    return 1 if failed else 0


def _scans(database: object, batch_id: str) -> list:
    from sqlalchemy import select

    from omr_scanner.database.models import BatchScan

    with database.session() as session:  # type: ignore[attr-defined]
        rows = session.scalars(select(BatchScan).where(BatchScan.batch_id == batch_id)).all()
        session.expunge_all()
        return list(rows)


if __name__ == "__main__":
    raise SystemExit(main())
