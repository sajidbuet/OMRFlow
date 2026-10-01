"""The OMRFlow process a crash test kills (0.1.1 phase 3).

Run by :mod:`tests.crash.harness` as a **separate operating-system process**
- never imported by a test - so that a forced termination is a real
``TerminateProcess`` of a real OMRFlow coordinator with a real worker pool,
not a Python exception that unwinds politely.

It drives the real application: a :class:`~omr_scanner.gui.main_window.MainWindow`
on the offscreen Qt platform, opening the project exactly as an operator does
(the project-open recovery sequence included), the real Scan stage (Add
Folder, Process All, Resume) and the real Resolve stage. The only thing it
adds is a :class:`~omr_scanner.services.run_hooks.RunHooks` implementation
injected into the Scan page, which

* appends every boundary to an **external** JSON-lines evidence log (flushed
  per line, so it survives the kill): ``submitted`` (the exact sheets handed
  to recognition), ``started``, ``committed`` (after the work unit's
  transaction returned), ``run_recognised``, ``review_state_completed``;
* at the boundary named by ``--pause``, writes ``paused`` and blocks forever,
  so the parent can kill it at an exact committed state.

Modal dialogs are answered by logging them (a modal would hang a headless
child); nothing here changes what OMRFlow reads or writes.

Actions:
    ``scan``     open; Resume the restored batch if there is one, else add
                 ``--scans`` and Process All; exit cleanly when finished.
    ``resolve``  open; decide ``--decisions`` conflicts through the Resolve
                 page (correct a value where one is allowed, else accept);
                 then exit cleanly (or pause).
    ``inspect``  open, log what Scan and Resolve reconstructed, exit cleanly.
    ``legacy_scan``  no window: write a batch exactly as a build *before*
                 0.1.1 phase 3 did - results committed in groups with **no**
                 conflicts (those were written only when a whole run ended) -
                 and pause after ``--pause-count`` sheets. The S1 boundary
                 "recognition committed, conflicts not yet generated".

``--clean-close-after N`` makes a ``scan`` run stop the way an operator's
"stop and exit" does once N sheets have committed: cancel, wait, close.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT / "src") not in sys.path:  # pragma: no cover - child bootstrap
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


class EvidenceLog:
    """Append-only JSON lines, flushed per event, written before anything waits on them."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: object) -> None:
        record = {"event": event, "pid": os.getpid(), "t": time.time(), **fields}
        with self._lock:
            self._handle.write(json.dumps(record, default=str) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())


class Hooks:
    """Writes each durable boundary to the evidence log; blocks at ``pause``."""

    def __init__(self, log: EvidenceLog, pause: str, pause_count: int) -> None:
        self.log = log
        self.pause = pause
        self.pause_count = pause_count
        self.committed_total = 0
        self.started_total = 0

    def _block(self, where: str) -> None:
        self.log.write("paused", at=where, committed=self.committed_total)
        while True:  # killed from outside; never returns
            time.sleep(3600)

    def submitted(self, batch_id: object, paths: object) -> None:
        self.log.write(
            "submitted", batch_id=batch_id, paths=[Path(item).name for item in paths]  # type: ignore[attr-defined]
        )

    def started(self, path: Path) -> None:
        self.started_total += 1
        self.log.write("started", path=Path(path).name)
        if self.pause == "started" and self.started_total > self.pause_count:
            self._block(f"started:{Path(path).name}")

    def committed(self, batch_id: object, outcomes: object) -> None:
        names = [Path(item.source_path).name for item in outcomes]  # type: ignore[attr-defined]
        self.committed_total += len(names)
        self.log.write("committed", batch_id=batch_id, paths=names)
        if self.pause == "committed" and self.committed_total >= self.pause_count:
            self._block("committed")

    def run_recognised(self, batch_id: object) -> None:
        self.log.write("run_recognised", batch_id=batch_id)
        if self.pause == "run_recognised":
            self._block("run_recognised")

    def review_state_completed(self, batch_id: object) -> None:
        self.log.write("review_state_completed", batch_id=batch_id)
        if self.pause == "review_state_completed":
            self._block("review_state_completed")


def _answer_modals(log: EvidenceLog) -> None:
    """Replace modal dialogs with log lines: a modal would hang a headless child."""
    from PySide6.QtWidgets import QMessageBox

    def answer(kind: str, result: object) -> object:
        def modal(
            _parent: object, title: str, text: str = "", *_a: object, **_k: object
        ) -> object:
            log.write("modal", kind=kind, title=title, text=text)
            return result

        return staticmethod(modal)

    QMessageBox.information = answer("information", QMessageBox.StandardButton.Ok)  # type: ignore[method-assign]
    QMessageBox.warning = answer("warning", QMessageBox.StandardButton.Cancel)  # type: ignore[method-assign]
    QMessageBox.critical = answer("critical", QMessageBox.StandardButton.Ok)  # type: ignore[method-assign]
    QMessageBox.question = answer("question", QMessageBox.StandardButton.Yes)  # type: ignore[method-assign]


def _reconstructed(window: object) -> dict[str, object]:
    """What Scan and Resolve show right after the project opened."""
    scan = window._scan_page()  # type: ignore[attr-defined]
    resolve = window._resolve_page()  # type: ignore[attr-defined]
    return {
        "scan_batch_id": scan.state.batch_id,
        "scan_entries": len(scan.state.entries),
        "scan_entries_with_result": sum(1 for e in scan.state.entries if e.processed is not None),
        "scan_counts_label": scan.progress_counts_label.text(),
        "scan_progress_label": scan.progress_label.text(),
        "scan_resume_enabled": scan.resume_button.isEnabled(),
        "scan_processing": scan.is_processing,
        "resolve_batch_id": resolve.state.batch_id,
        "resolve_queue": [item.conflict_id for item in resolve.state.conflicts],
        "resolve_summary": resolve.summary_label.text(),
    }


def _legacy_scan(arguments: argparse.Namespace, log: EvidenceLog) -> int:
    """The pre-phase-3 write path, killed before its run's conflict pass."""
    from omr_scanner.database.models import BatchStatus
    from omr_scanner.services import (
        batch_store,
        collect_scan_files,
        load_template,
        open_project,
        resolve_active_template,
    )
    from omr_scanner.services.batch_processor import BatchOptions, process_batch

    session = open_project(arguments.project)
    database = session.database
    template_path = resolve_active_template(session.project)
    assert template_path is not None
    template = load_template(template_path)
    paths = list(collect_scan_files([arguments.scans]))
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template, template_path)
    )
    batch_store.mark_queued(database, batch_id, paths)
    batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
    hooks = Hooks(log, "committed", arguments.pause_count)
    # No template: recognition only, in groups - what record_results did then.
    recorder = batch_store.BatchRecorder(
        database=database,
        batch_id=batch_id,
        flush_every=5,
        on_commit=lambda items: hooks.committed(batch_id, items),
    )
    hooks.submitted(batch_id, paths)
    process_batch(
        paths,
        template,
        options=BatchOptions(),
        on_result=recorder.record,
        workers=max(arguments.workers, 1),
    )
    recorder.flush()
    log.write("legacy_run_ended_without_pause")
    session.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument(
        "--action", choices=("scan", "resolve", "inspect", "legacy_scan"), required=True
    )
    parser.add_argument("--scans", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--pause",
        choices=("", "committed", "started", "run_recognised", "review_state_completed",
                 "decisions"),
        default="",
    )
    parser.add_argument("--pause-count", type=int, default=0)
    parser.add_argument("--clean-close-after", type=int, default=0)
    parser.add_argument("--decisions", type=int, default=0)
    parser.add_argument("--force-lock", action="store_true")
    arguments = parser.parse_args(argv)

    log = EvidenceLog(arguments.log)
    log.write("child_started", action=arguments.action, argv=sys.argv[1:])
    if arguments.action == "legacy_scan":
        return _legacy_scan(arguments, log)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from omr_scanner.config import AppConfig
    from omr_scanner.config.processing import ProcessingMode, ProcessingSettings
    from omr_scanner.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    _answer_modals(log)
    processing = ProcessingSettings(
        mode=ProcessingMode.SINGLE_CORE if arguments.workers <= 1 else ProcessingMode.CUSTOM,
        worker_count=max(arguments.workers, 1),
    )
    config = AppConfig(reviewer_name="Crash Harness", processing=processing)
    window = MainWindow(config=config, config_path=arguments.log.with_suffix(".config.json"))
    opened = (
        window.open_project_resolving_lock(arguments.project, action="force")
        if arguments.force_lock
        else window.open_project_at(arguments.project)
    )
    if not opened:
        log.write("open_failed")
        return 3
    log.write("opened", **_reconstructed(window))

    scan = window._scan_page()
    resolve = window._resolve_page()
    hooks = Hooks(log, arguments.pause, arguments.pause_count)
    scan.run_hooks = hooks
    exit_code = {"value": 0}

    def finish(code: int = 0) -> None:
        exit_code["value"] = code
        window.close()  # the ordinary close path: closeEvent, project released
        log.write("closed_cleanly", code=code)
        # Straight out once the application's own close path has run: the
        # offscreen Qt teardown at interpreter exit aborted the child
        # (0xC0000409 / 0xC0000005) while this harness was built, after the
        # project and its database had been released. Not what is under test.
        sys.stdout.flush()
        os._exit(code)

    if arguments.action == "inspect":
        QTimer.singleShot(0, finish)

    elif arguments.action == "scan":

        stop_requested = threading.Event()
        stopping = {"done": False}

        def on_finished(report: object) -> None:
            log.write(
                "run_finished",
                cancelled=bool(getattr(report, "cancelled", False)),
                total=int(getattr(report, "total", 0)),
                batch_id=scan.state.batch_id,
            )
            if stop_requested.is_set():
                QTimer.singleShot(0, stop_and_exit)
            else:
                QTimer.singleShot(0, finish)

        scan.batch_finished.connect(on_finished)

        if arguments.clean_close_after:
            original = hooks.committed

            def committed_then_close(batch_id: object, outcomes: object) -> None:
                original(batch_id, outcomes)
                reached = hooks.committed_total >= arguments.clean_close_after
                if reached and not stop_requested.is_set():
                    # The operator presses Cancel (a flag the run polls - safe
                    # from this thread), then exits; the window's own
                    # stop-and-exit path runs on the GUI thread.
                    worker = scan._worker
                    if worker is not None:
                        worker.cancel()
                    stop_requested.set()

            hooks.committed = committed_then_close  # type: ignore[method-assign]
            poll = QTimer()
            poll.setInterval(20)
            poll.timeout.connect(lambda: stop_requested.is_set() and stop_and_exit())
            poll.start()

        def stop_and_exit() -> None:
            # What the window does when the operator confirms "stop and exit"
            # (closeEvent -> shutdown_batch): cancel, wait for the run, settle.
            if stopping["done"]:
                return
            stopping["done"] = True
            was_running = scan.is_processing
            scan.shutdown_batch()
            log.write("stopped_by_operator", batch_id=scan.state.batch_id, was_running=was_running)
            finish()

        def start() -> None:
            if scan.state.batch_id is not None and scan.batch_summary() is not None and (
                scan.batch_summary().pending  # type: ignore[union-attr]
            ):
                log.write("resume_requested", batch_id=scan.state.batch_id)
                started = scan.resume_batch()
            else:
                if arguments.scans is None:
                    log.write("nothing_to_do")
                    finish()
                    return
                scan.add_scan_paths([arguments.scans])
                log.write("process_all_requested", entries=len(scan.state.entries))
                started = scan.process_all()
            log.write("run_started", started=bool(started), batch_id=scan.state.batch_id)
            if not started:
                finish(4)

        QTimer.singleShot(0, start)

    elif arguments.action == "resolve":

        def decide() -> None:
            made = 0
            window.show_page("resolve")
            while made < arguments.decisions:
                conflict = resolve.current_conflict()
                if conflict is None:
                    if not resolve.state.conflicts:
                        break
                    resolve.queue_table.selectRow(0)
                    conflict = resolve.current_conflict()
                    if conflict is None:
                        break
                if conflict.allows_value_correction:
                    resolve.reason_text.setPlainText("crash harness correction")
                    ok = resolve.correct("9")
                    action = "correct"
                else:
                    ok = resolve.accept_machine()
                    action = "accept"
                if not ok:
                    log.write("decision_refused", conflict_id=conflict.conflict_id)
                    break
                made += 1
                log.write(
                    "decision_committed",
                    conflict_id=conflict.conflict_id,
                    action=action,
                    count=made,
                )
                if arguments.pause == "decisions" and made >= arguments.pause_count:
                    hooks._block("decisions")
            log.write("decisions_done", count=made)
            finish()

        QTimer.singleShot(0, decide)

    app.exec()
    return exit_code["value"]


if __name__ == "__main__":  # pragma: no cover - child entry point
    code = main()
    # The application's own close path has already run (the window closed,
    # the project and its database released). Leaving through the
    # interpreter's normal teardown destroys the offscreen QApplication and
    # its widgets in an order that aborted the child with 0xC0000409 on
    # Windows while this harness was built - after `closed_cleanly` was
    # logged and with no database open. That is not what is under test, so
    # the child exits directly.
    sys.stdout.flush()
    os._exit(code)
