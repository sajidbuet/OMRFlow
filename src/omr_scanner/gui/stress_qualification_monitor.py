"""Watching a Phase 10 qualification campaign that is running elsewhere.

Purpose:
    Give an operator somewhere to look during an eight-hour unattended
    campaign, and the verdict when it ends - without the window being able to
    affect the thing it is watching.

Responsibilities:
    * Poll ``qualification_state.json`` and show the campaign's stages, the
      orchestrator's pid and whether it is alive.
    * Poll the tail of ``telemetry.csv`` and show the latest throughput,
      resource and disk figures, plus an ETA derived from the recent rate and
      labelled as an estimate.
    * Offer the four things an operator legitimately needs: stop cleanly
      between runs, force-kill (clearly marked as destructive and *not* the
      same as the campaign's own deliberate kills), read the report, and close
      the window without touching the campaign.

What does NOT belong here:
    * Writing anything into the campaign's directory, with exactly one
      deliberate exception: the ``stop_requested`` sentinel, which is the
      documented way to ask the orchestrator to stop between runs (see
      :func:`omr_scanner.evaluation.qualification.consume_stop_request`).
      Nothing else in this module writes, and it never opens a database.
    * Any judgement. "Qualified", "failed" and the failing assertion names are
      read out of the state file the harness wrote; this window has no opinion
      of its own and cannot reach a different verdict from the command line.

Why it must be safe to close:
    The campaign is a detached process. Closing this window, or OMRFlow
    itself, leaves it running - which is the point, and is why the Close
    button says so.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.stress_qualification_dialog import (
    LOCK_FILE_NAME,
    STATE_FILE_NAME,
    STOP_REQUEST_FILE_NAME,
    SUMMARY_MD_NAME,
    TELEMETRY_CSV_NAME,
    DetachedLauncher,
    build_resume_argv,
    format_bytes,
    is_pid_alive,
    launch_detached,
    orchestrator_pid,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

REFRESH_INTERVAL_MS = 3000
"""How often the state file and the telemetry tail are re-read.

Three seconds against a five-second telemetry interval: often enough to feel
live, rare enough that watching a campaign costs it nothing."""

TELEMETRY_TAIL_BYTES = 64 * 1024
"""How much of the end of ``telemetry.csv`` is read per refresh.

The file grows for hours; re-reading all of it every three seconds would make
the monitor the heaviest thing on the machine."""

TELEMETRY_TAIL_ROWS = 12
"""Samples kept for the rate used in the ETA - about a minute of history.

Long enough that one slow sample does not swing the estimate, short enough
that it follows a real change in throughput."""

RESUME_LOG_NAME = "gui_resume.log"

_STATUS_COLOR = {
    "qualified": "#1b7f3a",
    "passed_not_qualification": "#1b7f3a",
    "running": "#1f5c8b",
    "stopped": "#9a6a00",
    "interrupted": "#9a6a00",
    "failed": "#b3261e",
}


# ----------------------------------------------------------------------
# Reading the campaign's files
# ----------------------------------------------------------------------
def read_campaign_state(output_dir: Path) -> dict[str, Any]:
    """Read a campaign's state file, plus the two facts it cannot hold.

    Deliberately a plain :mod:`json` read rather than
    :func:`omr_scanner.evaluation.qualification.load_status`: that function
    lives in a module which imports SQLAlchemy and the database layer, and the
    GUI must not depend on either (``tests/unit/test_architecture.py``). The
    state file is written atomically by the harness precisely so that this
    read is safe while a campaign is writing it.

    Args:
        output_dir: The campaign's directory.

    Returns:
        The parsed state with ``exists``, ``readable``, ``orchestrator_pid``,
        ``orchestrator_running`` and ``stop_requested`` added. Never raises: a
        missing, half-written or unparseable file is a state to display, not
        an error to propagate into Qt's event loop.
    """
    pid = orchestrator_pid(output_dir)
    base: dict[str, Any] = {
        "output_dir": str(output_dir),
        "orchestrator_pid": pid,
        "orchestrator_running": is_pid_alive(pid),
        "stop_requested": (output_dir / STOP_REQUEST_FILE_NAME).is_file(),
        "lock_present": (output_dir / LOCK_FILE_NAME).is_file(),
        "summary_exists": (output_dir / SUMMARY_MD_NAME).is_file(),
    }
    state_path = output_dir / STATE_FILE_NAME
    if not state_path.is_file():
        return {**base, "exists": False, "readable": False}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "exists": True, "readable": False, "error": str(exc)}
    if not isinstance(payload, dict):
        return {**base, "exists": True, "readable": False, "error": "not a JSON object"}
    return {**payload, **base, "exists": True, "readable": True}


def run_ids_of(state: dict[str, Any]) -> list[str]:
    """The runs this campaign performs, derived from its recorded config.

    Mirrors :meth:`omr_scanner.evaluation.qualification.QualificationConfig.run_ids`
    because the state file stores the config rather than the derived list.
    """
    config = state.get("config") or {}
    if not isinstance(config, dict):
        return []
    if config.get("mode") == "reference":
        return ["R0"]
    checkpoints = config.get("checkpoints") or []
    try:
        kills = [f"K{int(percent):02d}" for percent in checkpoints]
    except (TypeError, ValueError):
        kills = []
    return ["R0", *kills]


def read_telemetry_tail(
    path: Path, *, rows: int = TELEMETRY_TAIL_ROWS
) -> list[dict[str, str]]:
    """Return the last ``rows`` complete telemetry samples.

    Reads only the end of the file, and drops the first line it finds there
    because a byte offset lands mid-row. A row with the wrong number of fields
    is skipped rather than guessed at: the campaign flushes after every sample
    but the last one can still be partial at the instant this reads it.
    """
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            header_bytes = handle.readline()
            handle.seek(0, io.SEEK_END)
            size = handle.tell()
            start = max(len(header_bytes), size - TELEMETRY_TAIL_BYTES)
            handle.seek(start)
            body = handle.read()
    except OSError:
        return []

    fields = next(csv.reader([header_bytes.decode("utf-8", "replace")]), [])
    if not fields:
        return []
    lines = body.decode("utf-8", "replace").splitlines()
    if start > len(header_bytes) and lines:
        lines = lines[1:]
    samples = [
        dict(zip(fields, row, strict=True))
        for row in csv.reader(lines)
        if len(row) == len(fields)
    ]
    return samples[-rows:]


def _as_float(value: object, default: float = 0.0) -> float:
    """A telemetry field as a number. Every CSV cell arrives as text."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def format_duration(seconds: float) -> str:
    """``"3 h 42 min"``.

    A local mirror of
    :func:`omr_scanner.evaluation.qualification.format_duration`, for the ETA
    this window derives from the telemetry it just read. Duplicated rather
    than imported for the layering reason given in
    :func:`read_campaign_state`.
    """
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = int(minutes // 60)
    return f"{hours} h {int(minutes - hours * 60):02d} min"


def estimate_remaining(state: dict[str, Any], samples: Sequence[dict[str, str]]) -> str:
    """An ETA for the run in progress, from the recent committed rate.

    For the *current run only*, and said so in the text. Extrapolating across
    five kill runs plus their re-done work would be a number nobody could
    check, and the preflight already gives a whole-campaign estimate from
    measured throughput.
    """
    if not samples:
        return "not yet measurable"
    sheets = int((state.get("config") or {}).get("sheets") or 0)
    latest = samples[-1]
    committed = _as_float(latest.get("committed"))
    rates = [_as_float(sample.get("sheets_per_second")) for sample in samples]
    positive = [rate for rate in rates if rate > 0]
    if not sheets or not positive:
        return "not yet measurable"
    remaining = max(sheets - committed, 0.0)
    rate = sum(positive) / len(positive)
    return (
        f"about {format_duration(remaining / rate)} left in "
        f"{latest.get('run_id') or 'this run'} "
        f"(estimate, from the last {len(positive)} sample(s) at {rate:.1f} sheets/s)"
    )


def failed_assertion_names(state: dict[str, Any]) -> list[str]:
    """Every failing assertion the state file records, run by run.

    Read out of the harness's own stage data (``failed_assertions``), never
    recomputed here - there is exactly one place that decides whether an
    assertion failed, and it is not the GUI.
    """
    names: list[str] = []
    stages = state.get("stages") or {}
    if not isinstance(stages, dict):
        return names
    # Ordered by the campaign's own run sequence where that is recoverable,
    # then every remaining stage. The second half is not redundancy: the run
    # list is derived from the recorded config, so a state file whose config
    # is missing or malformed would otherwise report "see the report for the
    # details" about a failure it is holding the details of.
    ordered = [run for run in run_ids_of(state) if run in stages]
    ordered += [name for name in stages if name not in ordered]
    for run_id in ordered:
        record = stages.get(run_id)
        if not isinstance(record, dict):
            continue
        data = record.get("data") or {}
        failed = data.get("failed_assertions") if isinstance(data, dict) else None
        if isinstance(failed, list):
            names.extend(f"{run_id}: {name}" for name in failed)
    return names


def verdict_text(state: dict[str, Any]) -> str:
    """The campaign's outcome in plain language.

    Three finished outcomes are distinguished on purpose, because conflating
    the middle one with the first is the exact claim Phase 10 must not make:
    a campaign can be *qualified*, or it can have *passed without being the
    release qualification*, or it can have *failed*.
    """
    if not state.get("exists"):
        return "No campaign found in this folder."
    if not state.get("readable"):
        return f"The campaign's state file could not be read: {state.get('error', 'unknown')}"

    status = str(state.get("overall_status", "unknown"))
    if status == "qualified":
        return (
            "QUALIFIED. Every run passed and this campaign was the full "
            "100,000-sheet release qualification."
        )
    if status == "passed_not_qualification":
        notes = [str(note) for note in state.get("notes") or ()]
        caveat = next((note for note in notes if "NOT the Phase 10" in note), "")
        return (
            "All runs passed, but this was NOT the release qualification. "
            + (caveat or "See the report for exactly why.")
        )
    if status == "failed":
        failures = failed_assertion_names(state)
        detail = ", ".join(failures) if failures else "see the report for the details"
        return f"FAILED. Release-blocking assertion(s): {detail}."
    if status == "stopped":
        return (
            "STOPPED between runs at your request. Nothing failed and nothing "
            "was interrupted; the remaining runs were never attempted. Resume "
            "to continue."
        )
    if status == "interrupted":
        return (
            "INTERRUPTED - the orchestrator stopped without finishing. The "
            "verified runs are kept; resume to continue."
        )
    if state.get("orchestrator_running"):
        pending = "stopping after the current run" if state.get("stop_requested") else "running"
        return f"Campaign {pending} (orchestrator PID {state.get('orchestrator_pid')})."
    return (
        f"Campaign status '{status}', but no orchestrator is alive. It was "
        "interrupted - resume to continue it."
    )


def summary_text(state: dict[str, Any], samples: Sequence[dict[str, str]]) -> str:
    """A short, pasteable summary of where the campaign has got to."""
    lines = [
        f"OMRFlow Phase 10 qualification - {state.get('output_dir', '?')}",
        f"Status: {state.get('overall_status', 'unknown')}",
        f"Verdict: {verdict_text(state)}",
        f"Started: {state.get('started_at', '?')}   Updated: {state.get('updated_at', '?')}",
        "",
        "Stages:",
    ]
    stages = state.get("stages") or {}
    if isinstance(stages, dict):
        for name, record in stages.items():
            if not isinstance(record, dict):
                continue
            lines.append(
                f"  {record.get('status', '?')!s:<9} {name:<10} "
                f"{record.get('detail', '')}"
            )
    if samples:
        latest = samples[-1]
        lines += [
            "",
            "Latest telemetry:",
            f"  committed {latest.get('committed', '?')} / "
            f"pending {latest.get('pending', '?')} / failed {latest.get('failed', '?')}",
            f"  {_as_float(latest.get('sheets_per_second')):.2f} sheets/s, "
            f"{latest.get('process_count', '?')} process(es), "
            f"{_as_float(latest.get('tree_cpu_percent')):.0f}% CPU, "
            f"{_as_float(latest.get('tree_memory_mb')):.0f} MB RSS",
            f"  database {format_bytes(_as_float(latest.get('database_bytes')))}, "
            f"disk free {format_bytes(_as_float(latest.get('disk_free_bytes')))}",
        ]
    notes = [str(note) for note in state.get("notes") or ()]
    if notes:
        lines += ["", "Notes:", *(f"  ! {note}" for note in notes)]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
class StressQualificationMonitor(QDialog):
    """Read-only monitor for one campaign, plus the controls an operator needs.

    Args:
        output_dir: The campaign's directory.
        parent: Optional Qt parent.
        launcher: How ``resume`` is started, if the operator asks for it.
            Injected so that no test starts a process.
        auto_refresh: Start the polling timer. Off in tests, which call
            :meth:`refresh` directly and assert on what it rendered.

    Testability:
        :meth:`refresh`, :meth:`request_stop_safely` and the module-level
        readers are all free of modals, so a synthetic
        ``qualification_state.json`` plus ``telemetry.csv`` in a temporary
        directory is enough to assert everything this window displays.
    """

    def __init__(
        self,
        output_dir: Path,
        parent: QWidget | None = None,
        *,
        launcher: DetachedLauncher = launch_detached,
        auto_refresh: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("stressQualificationMonitor")
        self.setWindowTitle("100,000-Sheet Stress Test - Monitor")
        # Deliberately not modal: an eight-hour campaign must not lock the
        # operator out of the rest of the application while it runs.
        self.setModal(False)
        self.resize(820, 700)

        self._output_dir = output_dir
        self._launcher = launcher
        self._state: dict[str, Any] = {}
        self._samples: list[dict[str, str]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_status_box())
        layout.addWidget(self._build_stage_box())
        layout.addWidget(self._build_telemetry_box())
        layout.addWidget(self._build_notes_box())
        layout.addWidget(self._build_buttons())

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self.refresh()
        if auto_refresh:
            self._timer.start()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_status_box(self) -> QGroupBox:
        box = QGroupBox("Campaign")
        layout = QVBoxLayout(box)

        self.folder_label = QLabel(str(self._output_dir))
        self.folder_label.setObjectName("stressMonitorFolderLabel")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("stressMonitorStatusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.verdict_label = QLabel("")
        self.verdict_label.setObjectName("stressMonitorVerdictLabel")
        self.verdict_label.setWordWrap(True)
        layout.addWidget(self.verdict_label)
        return box

    def _build_stage_box(self) -> QGroupBox:
        box = QGroupBox("Stages")
        layout = QVBoxLayout(box)
        self.stage_table = QTableWidget(0, 3)
        self.stage_table.setObjectName("stressMonitorStageTable")
        self.stage_table.setHorizontalHeaderLabels(("Stage", "Status", "Detail"))
        self.stage_table.verticalHeader().setVisible(False)
        self.stage_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stage_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.stage_table)
        return box

    def _build_telemetry_box(self) -> QGroupBox:
        box = QGroupBox("Latest measurement (read from telemetry.csv)")
        layout = QVBoxLayout(box)
        self.telemetry_label = QLabel("No telemetry yet.")
        self.telemetry_label.setObjectName("stressMonitorTelemetryLabel")
        self.telemetry_label.setWordWrap(True)
        layout.addWidget(self.telemetry_label)

        self.eta_label = QLabel("")
        self.eta_label.setObjectName("stressMonitorEtaLabel")
        self.eta_label.setWordWrap(True)
        layout.addWidget(self.eta_label)
        return box

    def _build_notes_box(self) -> QGroupBox:
        box = QGroupBox("Campaign notes")
        layout = QVBoxLayout(box)
        self.notes_label = QLabel("None.")
        self.notes_label.setObjectName("stressMonitorNotesLabel")
        self.notes_label.setWordWrap(True)
        layout.addWidget(self.notes_label)
        return box

    def _build_buttons(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.stop_button = QPushButton("Stop Safely")
        self.stop_button.setObjectName("stressMonitorStopButton")
        self.stop_button.setToolTip(
            "Let the run in progress finish and judge it, then stop before the "
            "next one. Nothing is killed and nothing is lost; the campaign can "
            "be resumed."
        )
        self.stop_button.clicked.connect(self._prompt_stop_safely)
        row.addWidget(self.stop_button)

        self.force_kill_button = QPushButton("Force Kill Current Processing Run")
        self.force_kill_button.setObjectName("stressMonitorForceKillButton")
        self.force_kill_button.setToolTip(
            "Destructive. Kills the orchestrator and its children now. This is "
            "NOT one of the campaign's own deliberate kills - the run in "
            "progress will almost certainly be recorded as failed."
        )
        self.force_kill_button.clicked.connect(self._prompt_force_kill)
        row.addWidget(self.force_kill_button)

        self.resume_button = QPushButton("Resume Campaign")
        self.resume_button.setObjectName("stressMonitorResumeButton")
        self.resume_button.setToolTip(
            "Continue an interrupted campaign. Runs already verified are not "
            "repeated."
        )
        self.resume_button.clicked.connect(self._prompt_resume)
        row.addWidget(self.resume_button)
        row.addStretch(1)
        outer.addLayout(row)

        second = QHBoxLayout()
        self.open_report_button = QPushButton("Open Report")
        self.open_report_button.setObjectName("stressMonitorOpenReportButton")
        self.open_report_button.clicked.connect(self.open_report)
        second.addWidget(self.open_report_button)

        self.open_folder_button = QPushButton("Open Results Folder")
        self.open_folder_button.setObjectName("stressMonitorOpenFolderButton")
        self.open_folder_button.clicked.connect(self.open_results_folder)
        second.addWidget(self.open_folder_button)

        self.copy_button = QPushButton("Copy Summary")
        self.copy_button.setObjectName("stressMonitorCopySummaryButton")
        self.copy_button.clicked.connect(self.copy_summary)
        second.addWidget(self.copy_button)
        second.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setObjectName("stressMonitorButtonBox")
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_button.setObjectName("stressMonitorCloseButton")
        close_button.setToolTip(
            "Closes this window only. The campaign keeps running in its own "
            "process - closing OMRFlow will not stop it either."
        )
        buttons.rejected.connect(self.reject)
        second.addWidget(buttons)
        outer.addLayout(second)

        self.close_note_label = QLabel(
            "Closing this window does not stop the campaign. It runs as its own "
            "detached process; reopen Tools > Developer / Testing > Run "
            "100,000-Sheet Stress Test to come back to it."
        )
        self.close_note_label.setObjectName("stressMonitorCloseNoteLabel")
        self.close_note_label.setWordWrap(True)
        outer.addWidget(self.close_note_label)
        return container

    # ------------------------------------------------------------------
    # Refreshing
    # ------------------------------------------------------------------
    def refresh(self) -> dict[str, Any]:
        """Re-read the campaign's files and redraw. Returns the state it read.

        Returned rather than only drawn so a test can assert against exactly
        what was rendered from.
        """
        state = read_campaign_state(self._output_dir)
        samples = read_telemetry_tail(self._output_dir / TELEMETRY_CSV_NAME)
        self._state, self._samples = state, samples

        running = bool(state.get("orchestrator_running"))
        status = str(state.get("overall_status", "unknown"))
        orchestrator = (
            f"running (PID {state.get('orchestrator_pid')})" if running else "not running"
        )
        pending_stop = (
            "    STOP REQUESTED - will stop after the current run"
            if state.get("stop_requested")
            else ""
        )
        self.status_label.setText(
            f"Status: {status}    Orchestrator: {orchestrator}{pending_stop}"
        )
        self.status_label.setStyleSheet(
            f"color: {_STATUS_COLOR.get(status, '#444444')}; font-weight: bold;"
        )
        self.verdict_label.setText(verdict_text(state))

        self._render_stages(state)
        self._render_telemetry(state, samples)

        notes = [str(note) for note in state.get("notes") or ()]
        self.notes_label.setText("\n".join(f"- {note}" for note in notes) or "None.")

        summary_exists = bool(state.get("summary_exists"))
        self.open_report_button.setEnabled(summary_exists)
        self.open_report_button.setToolTip(
            str(self._output_dir / SUMMARY_MD_NAME)
            if summary_exists
            else "The report appears when the campaign stops, successfully or not."
        )
        # Stopping and killing only mean anything while something is alive;
        # resuming only means anything when nothing is.
        self.stop_button.setEnabled(running and not bool(state.get("stop_requested")))
        self.force_kill_button.setEnabled(running)
        self.resume_button.setEnabled(
            bool(state.get("exists")) and bool(state.get("readable")) and not running
        )
        return state

    def _render_stages(self, state: dict[str, Any]) -> None:
        stages = state.get("stages") or {}
        rows = list(stages.items()) if isinstance(stages, dict) else []
        self.stage_table.setRowCount(len(rows))
        for row, (name, record) in enumerate(rows):
            detail = record.get("detail", "") if isinstance(record, dict) else ""
            status = record.get("status", "?") if isinstance(record, dict) else "?"
            for column, text in enumerate((str(name), str(status), str(detail))):
                self.stage_table.setItem(row, column, QTableWidgetItem(text))
        self.stage_table.resizeColumnsToContents()

    def _render_telemetry(
        self, state: dict[str, Any], samples: Sequence[dict[str, str]]
    ) -> None:
        if not samples:
            self.telemetry_label.setText("No telemetry yet.")
            self.eta_label.setText("")
            return
        latest = samples[-1]
        self.telemetry_label.setText(
            f"Run {latest.get('run_id', '?')} (attempt {latest.get('attempt', '?')}), "
            f"sampled {latest.get('timestamp', '?')}\n"
            f"Committed {latest.get('committed', '?')}    "
            f"Pending {latest.get('pending', '?')}    "
            f"Failed {latest.get('failed', '?')}    "
            f"Rows {latest.get('total_rows', '?')}\n"
            f"{_as_float(latest.get('sheets_per_second')):.2f} sheets/s    "
            f"Worker pool: {_as_float(latest.get('tree_cpu_percent')):.0f}% CPU, "
            f"{_as_float(latest.get('tree_memory_mb')):.0f} MB RSS across "
            f"{latest.get('process_count', '?')} process(es)\n"
            f"Database {format_bytes(_as_float(latest.get('database_bytes')))}    "
            f"Disk free {format_bytes(_as_float(latest.get('disk_free_bytes')))}"
        )
        self.eta_label.setText(f"ETA: {estimate_remaining(state, samples)}")

    @property
    def state(self) -> dict[str, Any]:
        """The state read by the last :meth:`refresh`."""
        return self._state

    @property
    def samples(self) -> list[dict[str, str]]:
        """The telemetry samples read by the last :meth:`refresh`."""
        return self._samples

    # ------------------------------------------------------------------
    # Stopping
    # ------------------------------------------------------------------
    def request_stop_safely(self) -> Path:
        """Write the ``stop_requested`` sentinel. No dialog - testable directly.

        The one thing this window writes. The orchestrator checks for it
        between runs, records that it was asked to stop, removes it and exits
        cleanly with a resumable campaign - see
        :func:`omr_scanner.evaluation.qualification.consume_stop_request`. It
        is never confused with a failure.

        Returns:
            The sentinel's path.
        """
        path = self._output_dir / STOP_REQUEST_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Requested from the OMRFlow GUI monitor. The campaign stops after "
            "the run in progress has finished and been judged.\n",
            encoding="utf-8",
        )
        self.refresh()
        return path

    def _prompt_stop_safely(self) -> None:
        """Confirm, then ask the campaign to stop between runs."""
        answer = QMessageBox.question(
            self,
            "Stop after the current run?",
            "The run in progress will finish and be judged, and then the "
            "campaign will stop before starting the next one.\n\n"
            "Nothing is killed, nothing measured is lost, and this is not "
            "recorded as a failure. You can continue later with Resume "
            "Campaign.\n\n"
            "A 100,000-sheet run can take hours to finish, so the campaign may "
            "not stop immediately.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.request_stop_safely()

    def force_kill_now(self) -> tuple[int, ...]:
        """Kill the orchestrator and its children. Returns the pids killed.

        No dialog, so it is testable - but nothing in the test suite calls it
        against a real process. :mod:`psutil` is imported here rather than at
        module scope to keep it out of the GUI's import graph for every window
        that never needs it.
        """
        pid = orchestrator_pid(self._output_dir)
        if pid is None:
            return ()
        import psutil

        killed: list[int] = []
        try:
            parent = psutil.Process(int(pid))
            victims = [*parent.children(recursive=True), parent]
        except psutil.Error:
            return ()
        for victim in victims:
            try:
                victim.kill()
                killed.append(int(victim.pid))
            except psutil.Error:  # pragma: no cover - race with exit
                continue
        self.refresh()
        return tuple(killed)

    def _prompt_force_kill(self) -> None:
        """Confirm loudly, then force-kill the campaign."""
        answer = QMessageBox.warning(
            self,
            "Force kill the campaign?",
            "This kills the orchestrator and every process under it right "
            "now.\n\n"
            "This is NOT one of the campaign's own deliberate kills. Those are "
            "measured, with the committed work recorded first; this one is "
            "not, so the run in progress will almost certainly be recorded as "
            "failed and its evidence will be incomplete.\n\n"
            "Use Stop Safely unless something is actually wrong.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.force_kill_now()

    # ------------------------------------------------------------------
    # Resuming
    # ------------------------------------------------------------------
    def resume_campaign(self) -> int | None:
        """Launch ``resume`` for this campaign. No dialog - testable directly."""
        pid = self._launcher(
            build_resume_argv(self._output_dir), self._output_dir / RESUME_LOG_NAME
        )
        self.refresh()
        return int(pid) if pid else None

    def _prompt_resume(self) -> None:
        """Confirm, then continue an interrupted campaign."""
        answer = QMessageBox.question(
            self,
            "Resume the campaign?",
            "The campaign continues from the first run that has not yet "
            "passed. Runs already verified are not repeated.\n\n"
            "It starts as its own detached process and will run unattended for "
            "hours.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.resume_campaign()

    # ------------------------------------------------------------------
    # Reading the results
    # ------------------------------------------------------------------
    def open_report(self) -> bool:
        """Open ``qualification_summary.md`` in whatever reads Markdown here."""
        path = self._output_dir / SUMMARY_MD_NAME
        if not path.is_file():
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))

    def open_results_folder(self) -> bool:
        """Open the campaign's directory in the file manager."""
        if not self._output_dir.is_dir():
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_dir))))

    def summary(self) -> str:
        """The short text :meth:`copy_summary` puts on the clipboard."""
        return summary_text(self._state, self._samples)

    def copy_summary(self) -> str:
        """Copy the summary to the clipboard and return what was copied."""
        text = self.summary()
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        return text

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def done(self, result: int) -> None:
        """Stop polling, and nothing else.

        Closing this window must never touch the campaign; the only thing it
        owns is the timer.
        """
        self._timer.stop()
        super().done(result)


__all__ = [
    "REFRESH_INTERVAL_MS",
    "RESUME_LOG_NAME",
    "TELEMETRY_TAIL_ROWS",
    "StressQualificationMonitor",
    "estimate_remaining",
    "failed_assertion_names",
    "format_duration",
    "read_campaign_state",
    "read_telemetry_tail",
    "run_ids_of",
    "summary_text",
    "verdict_text",
]
