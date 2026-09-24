"""Launching the Phase 10 100,000-sheet qualification from the menu.

Purpose:
    Let an operator start the release qualification without remembering a
    command line, and refuse to start one that cannot finish - while leaving
    every decision about the campaign itself to the headless tool.

Responsibilities:
    * The launch form: output directory, template, and which of the two
      campaign shapes to run.
    * Showing what the *headless* preflight concluded, by running
      ``phase10_qualification preflight`` and reading the ``preflight.json``
      it writes.
    * Starting ``phase10_qualification run`` as a fully detached process, and
      handing its output directory back to the caller.

What does NOT belong here:
    * Any stress-test, assertion or reporting logic. This dialog is a
      launcher and nothing else. Every number it shows was written to a file
      by :mod:`omr_scanner.evaluation.qualification`, so the GUI cannot
      possibly show a different verdict from the command line.
    * Importing :mod:`omr_scanner.evaluation.qualification`. That module
      reaches into SQLAlchemy and the database layer, which
      ``tests/unit/test_architecture.py`` forbids this layer from depending
      on - and the layering is right: a *launcher* has no business owning the
      engine it launches. The handful of file names and defaults it needs are
      restated below, next to a pointer at the source of truth.

Why the campaign is a detached process and not a worker thread:
    The campaign runs for many hours and deliberately force-kills processes.
    Closing OMRFlow - or OMRFlow crashing - must not stop it, so the child is
    started in its own process group, detached, with its console output
    redirected into the campaign's own directory. That is the whole point of
    the design and is asserted by
    :mod:`tests.gui.test_stress_qualification_gui`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ----------------------------------------------------------------------
# What the harness calls its files
# ----------------------------------------------------------------------
# Restated, not imported. The source of truth is
# `omr_scanner.evaluation.qualification` (STATE_FILE_NAME, LOCK_NAME,
# SUMMARY_MD_NAME, PREFLIGHT_JSON_NAME, STOP_REQUEST_FILE_NAME,
# DEFAULT_SHEETS, DEFAULT_CHECKPOINTS) and
# `omr_scanner.tools.phase10_qualification`. Importing that module here would
# drag SQLAlchemy and the database layer into the GUI, which the architecture
# test forbids; six short strings duplicated with this note is the smaller
# evil. If any of them changes, the GUI stops finding a campaign it did not
# start - visibly, not silently.
QUALIFICATION_MODULE = "omr_scanner.tools.phase10_qualification"
STATE_FILE_NAME = "qualification_state.json"
LOCK_FILE_NAME = "qualification.lock"
SUMMARY_MD_NAME = "qualification_summary.md"
PREFLIGHT_JSON_NAME = "preflight.json"
TELEMETRY_CSV_NAME = "telemetry.csv"
STOP_REQUEST_FILE_NAME = "stop_requested"

LAUNCH_LOG_NAME = "gui_launch.log"
"""Where the detached campaign's stdout and stderr go.

Inside the campaign's own output directory, because a detached process has no
console to inherit and losing its first traceback would make a campaign that
died on startup indistinguishable from one that never started."""

DEFAULT_SHEETS = 100_000
DEFAULT_CHECKPOINT_COUNT = 5
DEFAULT_TEMPLATE_RELATIVE = Path("examples/templates/100_question_4_choice_example.omrt")

DEFAULT_OUTPUT_DIR_NAME = "OMRFlow-qualification"

PREFLIGHT_TIMEOUT_SECONDS = 600.0
"""Ceiling for one preflight, not a delay.

It renders three sample sheets to prove the deterministic generator really is
deterministic, so a few seconds is normal and ten minutes means something is
badly wrong."""


class CampaignMode(StrEnum):
    """The two campaign shapes this dialog offers.

    ``progressive`` exists in the CLI as an engineering aid and is
    deliberately *not* offered here: it is never the release qualification and
    a menu item is exactly where somebody would pick it by accident.
    """

    FULL = "full"
    REFERENCE = "reference"


class CampaignSituation(StrEnum):
    """What an output directory already contains.

    Attributes:
        NONE: No campaign here; the launch form applies.
        RUNNING: A campaign whose orchestrator is alive - reconnect to it, do
            not start a second one in the same directory.
        RESUMABLE: A campaign that stopped before finishing, with no live
            orchestrator.
        FINISHED: Every run passed; there is nothing to resume.
        UNREADABLE: A state file that will not parse.
    """

    NONE = "none"
    RUNNING = "running"
    RESUMABLE = "resumable"
    FINISHED = "finished"
    UNREADABLE = "unreadable"


MODE_HEADLINE = {
    CampaignMode.FULL: "Full qualification (recommended)",
    CampaignMode.REFERENCE: "Single 100,000-sheet run",
}

MODE_DESCRIPTION = {
    CampaignMode.FULL: (
        "One uninterrupted 100,000-sheet reference run, then five independent "
        f"forced-kill runs ({DEFAULT_CHECKPOINT_COUNT} checkpoints, one fresh "
        "project each). This is the only mode that qualifies the release."
    ),
    CampaignMode.REFERENCE: (
        "The reference run alone: 100,000 sheets, no kills, no recovery "
        "testing. This is NOT the release qualification - it proves throughput "
        "and stability, not that a killed run recovers, so its report says so "
        "and must not be cited as the qualification."
    ),
}

UNATTENDED_WARNING = (
    "This campaign runs unattended for many hours and deliberately force-kills "
    "processing runs to test recovery. Leave the machine on reliable power with "
    "automatic sleep disabled, and do not run other heavy work on it - "
    "throughput measured on a contended machine is not comparable. OMRFlow does "
    "not change any power setting for you. Closing OMRFlow will NOT stop the "
    "campaign; it runs as its own process."
)


# ----------------------------------------------------------------------
# What the operator asked for
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CampaignRequest:
    """One complete answer to "which campaign, where".

    Attributes:
        output_dir: The directory the campaign will own entirely.
        template_path: The ``.omrt`` every run reads sheets with.
        mode: :class:`CampaignMode`.
        restart: Overwrite a campaign that already exists in ``output_dir``
            rather than resuming it.
    """

    output_dir: Path
    template_path: Path
    mode: CampaignMode = CampaignMode.FULL
    restart: bool = False


# ----------------------------------------------------------------------
# The command lines, as pure functions
# ----------------------------------------------------------------------
def build_preflight_argv(
    request: CampaignRequest, *, python_executable: str | None = None
) -> list[str]:
    """Return the argv that makes the headless tool write ``preflight.json``.

    A pure function so that a test can assert on the exact command without a
    process ever being started.
    """
    return [
        python_executable or sys.executable,
        "-m",
        QUALIFICATION_MODULE,
        "preflight",
        "--output-dir",
        str(request.output_dir),
        "--template",
        str(request.template_path),
        "--mode",
        str(request.mode),
    ]


def build_run_argv(
    request: CampaignRequest, *, python_executable: str | None = None
) -> list[str]:
    """Return the argv that starts the campaign.

    Sheets, seed and checkpoints are deliberately left off: omitting them
    takes the harness's own defaults (100,000 sheets, the fixed qualification
    seed, the five mandated checkpoints), so the GUI cannot quietly launch
    something smaller than the release qualification while calling it one.
    """
    argv = [
        python_executable or sys.executable,
        "-m",
        QUALIFICATION_MODULE,
        "run",
        "--output-dir",
        str(request.output_dir),
        "--template",
        str(request.template_path),
        "--mode",
        str(request.mode),
    ]
    if request.restart:
        argv.append("--restart")
    return argv


def build_resume_argv(output_dir: Path, *, python_executable: str | None = None) -> list[str]:
    """Return the argv that continues an interrupted campaign."""
    return [
        python_executable or sys.executable,
        "-m",
        QUALIFICATION_MODULE,
        "resume",
        "--output-dir",
        str(output_dir),
    ]


# ----------------------------------------------------------------------
# Starting and inspecting, as seams
# ----------------------------------------------------------------------
DetachedLauncher = Callable[[Sequence[str], Path], int]
"""``(argv, log_path) -> pid``. Injected so no test ever starts a campaign."""

PreflightRunner = Callable[[Sequence[str]], int]
"""``(argv) -> exit code``, run to completion. Injected for the same reason."""


def launch_detached(argv: Sequence[str], log_path: Path) -> int:
    """Start ``argv`` as a fully independent process and return its pid.

    Detached on purpose, and this is the single most important line in the
    feature: the campaign must survive the GUI being closed, crashing, or
    being killed by the operator. On Windows that means a new process group
    and ``DETACHED_PROCESS`` so no console handle is shared; the flags are
    resolved with :func:`getattr` so this module still imports on Linux and
    macOS, where a process started this way is already independent enough.

    Args:
        argv: The command to run.
        log_path: File the child's stdout and stderr are appended to.

    Returns:
        The child's process id.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    # Not a context manager: the handle must outlive this function, because
    # the child writes to it for hours after we have returned.
    handle = log_path.open("ab")
    process = subprocess.Popen(
        list(argv),
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )
    return int(process.pid)


def run_preflight_process(argv: Sequence[str]) -> int:
    """Run a preflight to completion and return its exit code.

    Blocking by design - it is called on a worker thread - and its exit code
    is only advisory here: what the dialog actually shows is the
    ``preflight.json`` the tool wrote, because that is what the campaign
    itself will read.
    """
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
        check=False,
    )
    return int(completed.returncode)


def default_output_dir() -> Path:
    """A sensible place to put a campaign, before the operator picks one.

    Under the home directory rather than beside the project: a campaign is
    tens of gigabytes of throwaway databases and has no business inside a
    source tree. Whether the drive is actually big enough is not guessed here
    - the preflight measures it and blocks the Start button if it is not.
    """
    return Path.home() / DEFAULT_OUTPUT_DIR_NAME


def default_template_path() -> Path:
    """The template the CLI itself defaults to, resolved against the repo.

    Returns the packaged example when it exists and the bare relative path
    otherwise, which the preflight will then report as unreadable rather than
    failing silently four hours in.
    """
    # src/omr_scanner/gui/<this file> -> repository root
    root = Path(__file__).resolve().parents[3]
    candidate = root / DEFAULT_TEMPLATE_RELATIVE
    return candidate if candidate.is_file() else DEFAULT_TEMPLATE_RELATIVE


def read_preflight(output_dir: Path) -> dict[str, Any] | None:
    """Read ``preflight.json``, or ``None`` when it is absent or unparseable.

    Plain :mod:`json`, because the GUI must be able to read a campaign's files
    without importing the engine that wrote them.
    """
    path = output_dir / PREFLIGHT_JSON_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def orchestrator_pid(output_dir: Path) -> int | None:
    """The pid recorded in the campaign lock file, if there is one."""
    path = output_dir / LOCK_FILE_NAME
    if not path.is_file():
        return None
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def is_pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` is a live process.

    :mod:`psutil` rather than ``os.kill(pid, 0)``: the project already depends
    on it for exactly this question (see
    :mod:`omr_scanner.services.project_lock`), and on Windows the signal trick
    does not answer it. It is a leaf dependency - no OpenCV, no database, no
    Qt - so the layering the architecture test enforces is untouched.
    """
    if pid is None:
        return False
    import psutil

    try:
        return bool(psutil.pid_exists(int(pid)))
    except (OSError, ValueError):  # pragma: no cover - platform edge case
        return False


def campaign_situation(output_dir: Path) -> CampaignSituation:
    """Classify what is already in ``output_dir``, read-only.

    This is what makes reopening the menu item reconnect to a running
    campaign instead of offering to start a second one on top of it.
    """
    state_path = output_dir / STATE_FILE_NAME
    if not state_path.is_file():
        return CampaignSituation.NONE
    if is_pid_alive(orchestrator_pid(output_dir)):
        return CampaignSituation.RUNNING
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CampaignSituation.UNREADABLE
    if not isinstance(payload, dict):
        return CampaignSituation.UNREADABLE
    stages = payload.get("stages") or {}
    runs = (payload.get("config") or {}).get("run_ids")
    if not isinstance(runs, list):
        # The state file records the config, not the derived run list, so the
        # run ids are recovered the same way the harness derives them.
        config = payload.get("config") or {}
        checkpoints = config.get("checkpoints") or []
        runs = ["R0"] + (
            []
            if config.get("mode") == "reference"
            else [f"K{int(percent):02d}" for percent in checkpoints]
        )
    every_run_passed = bool(runs) and all(
        (stages.get(str(run)) or {}).get("status") == "passed" for run in runs
    )
    return CampaignSituation.FINISHED if every_run_passed else CampaignSituation.RESUMABLE


def format_bytes(count: float) -> str:
    """``"12.4 GB"``.

    A local mirror of :func:`omr_scanner.evaluation.qualification.format_bytes`
    for the one number the harness does not pre-format: the disk *shortfall*,
    which is a subtraction of two figures it does provide. Every other size
    shown comes straight out of ``preflight.json`` as text.
    """
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable


# ----------------------------------------------------------------------
# Running the preflight off the GUI thread
# ----------------------------------------------------------------------
class PreflightWorker(QThread):
    """Run one headless preflight and hand back what it wrote.

    Signals:
        finished_preflight: ``dict`` - the parsed ``preflight.json``.
        failed: ``str`` - why no preflight could be read.

    Args:
        argv: The command to run, from :func:`build_preflight_argv`.
        output_dir: Where ``preflight.json`` will appear.
        runner: The seam that actually runs the process.
        parent: Optional Qt parent.

    A thread rather than a blocking call because the preflight starts a Python
    interpreter and renders three sample sheets; a few seconds of a frozen
    window every time the operator changes the mode is not acceptable.
    """

    finished_preflight = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        argv: Sequence[str],
        output_dir: Path,
        runner: PreflightRunner,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._argv = list(argv)
        self._output_dir = output_dir
        self._runner = runner

    def run(self) -> None:
        """Run the preflight, turning every failure into a signal.

        An exception escaping here would take the Qt thread down without
        telling anybody why, which is how a dialog ends up showing stale
        estimates for a directory that cannot be written to.
        """
        try:
            self._runner(self._argv)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        payload = read_preflight(self._output_dir)
        if payload is None:
            self.failed.emit(
                f"The preflight wrote no readable {PREFLIGHT_JSON_NAME} in "
                f"{self._output_dir}."
            )
            return
        self.finished_preflight.emit(payload)


# ----------------------------------------------------------------------
# The dialog
# ----------------------------------------------------------------------
class StressQualificationDialog(QDialog):
    """Ask what to qualify, show the headless preflight, then launch it.

    Args:
        parent: Optional Qt parent.
        output_dir: Directory to start from. Defaults to
            :func:`default_output_dir`.
        template_path: Template to start from. Defaults to
            :func:`default_template_path`.
        launcher: How the campaign is started. Injected so that a test can
            assert on the argv without a multi-hour process appearing.
        preflight_runner: How a preflight is run, for the same reason.
        auto_preflight: Run a preflight as soon as the dialog opens. Off in
            tests, which drive :meth:`start_preflight` explicitly.

    Testability:
        :meth:`request` and :meth:`apply_preflight` are pure with respect to
        Qt - no modal, no process - so the argv, the Start button's enabled
        state and the blocking message are all assertable directly.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        output_dir: Path | None = None,
        template_path: Path | None = None,
        launcher: DetachedLauncher = launch_detached,
        preflight_runner: PreflightRunner = run_preflight_process,
        auto_preflight: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("stressQualificationDialog")
        self.setWindowTitle("Run 100,000-Sheet Stress Test")
        self.setModal(True)
        self.resize(720, 680)

        self._launcher = launcher
        self._preflight_runner = preflight_runner
        self._worker: PreflightWorker | None = None
        self._preflight: dict[str, Any] | None = None
        self._launched_pid: int | None = None
        self._launched_request: CampaignRequest | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_warning_label())
        layout.addWidget(self._build_target_box(output_dir, template_path))
        layout.addWidget(self._build_mode_box())
        layout.addWidget(self._build_preflight_box())

        self._blocking_label = QLabel("")
        self._blocking_label.setObjectName("stressBlockingLabel")
        self._blocking_label.setWordWrap(True)
        self._blocking_label.setStyleSheet("color: #b3261e; font-weight: bold;")
        layout.addWidget(self._blocking_label)

        layout.addWidget(self._build_buttons())
        self._set_start_enabled(enabled=False, reason="Preflight has not run yet.")
        if auto_preflight:
            self.start_preflight()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_warning_label(self) -> QLabel:
        label = QLabel(UNATTENDED_WARNING)
        label.setObjectName("stressUnattendedWarningLabel")
        label.setWordWrap(True)
        label.setStyleSheet("color: #9a6a00; font-weight: bold;")
        return label

    def _build_target_box(
        self, output_dir: Path | None, template_path: Path | None
    ) -> QGroupBox:
        box = QGroupBox("Where and with what")
        form = QFormLayout(box)

        self.output_edit = QLineEdit(str(output_dir or default_output_dir()))
        self.output_edit.setObjectName("stressOutputEdit")
        self.output_edit.setToolTip(
            "The campaign owns this directory completely. Put it on a drive "
            "with tens of gigabytes free."
        )
        # Re-preflight when the operator finishes editing rather than on every
        # keystroke: each preflight is a process start plus three rendered
        # sheets, and one per typed character would be absurd.
        self.output_edit.editingFinished.connect(self.start_preflight)
        browse_output = QPushButton("Browse...")
        browse_output.setObjectName("stressBrowseOutputButton")
        browse_output.clicked.connect(self._prompt_output_dir)
        form.addRow("Output folder:", _with_button(self.output_edit, browse_output))

        self.template_edit = QLineEdit(str(template_path or default_template_path()))
        self.template_edit.setObjectName("stressTemplateEdit")
        self.template_edit.editingFinished.connect(self.start_preflight)
        browse_template = QPushButton("Browse...")
        browse_template.setObjectName("stressBrowseTemplateButton")
        browse_template.clicked.connect(self._prompt_template)
        form.addRow("Template:", _with_button(self.template_edit, browse_template))

        self.restart_checkbox = QCheckBox(
            "Start over, discarding the campaign already in this folder"
        )
        self.restart_checkbox.setObjectName("stressRestartCheckBox")
        self.restart_checkbox.setToolTip(
            "Only needed when this folder already holds a campaign you do not "
            "want to resume. Previous evidence is left on disk for you."
        )
        form.addRow("", self.restart_checkbox)
        return box

    def _build_mode_box(self) -> QGroupBox:
        box = QGroupBox("What to run")
        layout = QVBoxLayout(box)

        self.full_radio = QRadioButton(MODE_HEADLINE[CampaignMode.FULL])
        self.full_radio.setObjectName("stressFullModeRadio")
        self.full_radio.setChecked(True)
        self.full_radio.toggled.connect(self._on_mode_changed)
        layout.addWidget(self.full_radio)
        layout.addWidget(
            _description_label(MODE_DESCRIPTION[CampaignMode.FULL], "stressFullModeLabel")
        )

        self.reference_radio = QRadioButton(MODE_HEADLINE[CampaignMode.REFERENCE])
        self.reference_radio.setObjectName("stressReferenceModeRadio")
        layout.addWidget(self.reference_radio)
        layout.addWidget(
            _description_label(
                MODE_DESCRIPTION[CampaignMode.REFERENCE], "stressReferenceModeLabel"
            )
        )
        return box

    def _build_preflight_box(self) -> QGroupBox:
        box = QGroupBox("Preflight (run by the headless tool, not by this window)")
        layout = QVBoxLayout(box)

        self.estimate_label = QLabel("Running preflight...")
        self.estimate_label.setObjectName("stressEstimateLabel")
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        self.checks_table = QTableWidget(0, 3)
        self.checks_table.setObjectName("stressChecksTable")
        self.checks_table.setHorizontalHeaderLabels(("Check", "Result", "Detail"))
        self.checks_table.verticalHeader().setVisible(False)
        self.checks_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.checks_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.checks_table)
        return box

    def _build_buttons(self) -> QDialogButtonBox:
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("stressButtonBox")
        self.start_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.start_button.setObjectName("stressStartButton")
        self.start_button.setText("Start Campaign")
        self.start_button.setAutoDefault(False)
        self.start_button.setDefault(False)

        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("stressCancelButton")
        # Cancel is the default button, deliberately. Enter is pressed by
        # accident; a multi-hour campaign that force-kills processes is not
        # something Enter should ever be able to start.
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)

        self.refresh_button = buttons.addButton(
            "Re-run Preflight", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.refresh_button.setObjectName("stressRefreshPreflightButton")
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.clicked.connect(self.start_preflight)

        buttons.accepted.connect(self._on_start)
        buttons.rejected.connect(self.reject)
        return buttons

    # ------------------------------------------------------------------
    # The form
    # ------------------------------------------------------------------
    def selected_mode(self) -> CampaignMode:
        """Which campaign shape is selected."""
        return CampaignMode.FULL if self.full_radio.isChecked() else CampaignMode.REFERENCE

    def request(self) -> CampaignRequest | None:
        """What the operator has asked for, or ``None`` if the form is incomplete."""
        output = self.output_edit.text().strip()
        template = self.template_edit.text().strip()
        if not output or not template:
            return None
        return CampaignRequest(
            output_dir=Path(output).expanduser(),
            template_path=Path(template).expanduser(),
            mode=self.selected_mode(),
            restart=self.restart_checkbox.isChecked(),
        )

    def _prompt_output_dir(self) -> None:
        """Ask where the campaign should live. Owns its file dialog."""
        start = self.output_edit.text().strip() or str(default_output_dir())
        directory = QFileDialog.getExistingDirectory(
            self, "Select the qualification output folder", start
        )
        if directory:
            self.output_edit.setText(directory)
            self.start_preflight()

    def _prompt_template(self) -> None:
        """Ask which template the runs read. Owns its file dialog."""
        start = self.template_edit.text().strip() or str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select the template", start, "OMRFlow templates (*.omrt)"
        )
        if path:
            self.template_edit.setText(path)
            self.start_preflight()

    def _on_mode_changed(self) -> None:
        """A different mode is a different campaign, so re-measure it.

        The runtime and disk estimates for ``full`` are roughly six times
        those for ``reference``; showing one while the other is selected would
        be worse than showing nothing.
        """
        self.start_preflight()

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------
    def start_preflight(self) -> bool:
        """Ask the headless tool to preflight the current form.

        Returns:
            ``True`` when a preflight was started, ``False`` when the form is
            incomplete or one is already running.
        """
        request = self.request()
        if request is None:
            self._set_start_enabled(
                enabled=False, reason="Choose an output folder and a template."
            )
            return False
        if self._worker is not None and self._worker.isRunning():
            return False

        self.estimate_label.setText("Running preflight...")
        self._set_start_enabled(enabled=False, reason="")
        self.refresh_button.setEnabled(False)
        worker = PreflightWorker(
            build_preflight_argv(request), request.output_dir, self._preflight_runner, self
        )
        worker.finished_preflight.connect(self._on_preflight_ready)
        worker.failed.connect(self._on_preflight_failed)
        self._worker = worker
        worker.start()
        return True

    def _on_preflight_ready(self, payload: object) -> None:
        self.refresh_button.setEnabled(True)
        if isinstance(payload, dict):
            self.apply_preflight(payload)

    def _on_preflight_failed(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self._preflight = None
        self.estimate_label.setText("No estimates: the preflight could not be read.")
        self.checks_table.setRowCount(0)
        self._set_start_enabled(enabled=False, reason=message)

    def apply_preflight(self, payload: dict[str, Any]) -> None:
        """Display a ``preflight.json`` payload and gate the Start button on it.

        Everything shown here was decided by the headless tool. The only thing
        computed in the GUI is the disk *shortfall*, which is a subtraction of
        two numbers the payload already carries.

        Args:
            payload: The parsed ``preflight.json``.
        """
        self._preflight = payload
        checks = [check for check in payload.get("checks", ()) if isinstance(check, dict)]
        self.checks_table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            passed = bool(check.get("passed"))
            blocking = bool(check.get("blocking", True))
            mark = "PASS" if passed else ("FAIL" if blocking else "WARN")
            for column, text in enumerate(
                (str(check.get("name", "")), mark, str(check.get("detail", "")))
            ):
                self.checks_table.setItem(row, column, QTableWidgetItem(text))
        self.checks_table.resizeColumnsToContents()

        runs = payload.get("runs") or ()
        self.estimate_label.setText(
            f"Runs: {', '.join(str(run) for run in runs) or 'unknown'}\n"
            f"Estimated runtime: {payload.get('estimated_runtime_text', 'unknown')} "
            "(an estimate from previously measured throughput on this machine's "
            "project, not a guarantee)\n"
            f"Estimated peak disk: {payload.get('estimated_peak_disk_text', 'unknown')}   "
            f"Available now: {payload.get('available_disk_text', 'unknown')}"
        )

        failed_blocking = [
            str(check.get("name", "?"))
            for check in checks
            if not check.get("passed") and check.get("blocking", True)
        ]
        if not payload.get("disk_sufficient", True):
            shortfall = max(
                float(payload.get("estimated_peak_disk_bytes", 0))
                - float(payload.get("available_disk_bytes", 0)),
                0.0,
            )
            self._set_start_enabled(
                enabled=False,
                reason=(
                    "Not enough disk space: the campaign needs about "
                    f"{payload.get('estimated_peak_disk_text', 'more')} at its peak and "
                    f"{payload.get('available_disk_text', 'less')} is free - "
                    f"{format_bytes(shortfall)} short. Free space or choose a "
                    "folder on a larger drive."
                ),
            )
            return
        if failed_blocking:
            self._set_start_enabled(
                enabled=False,
                reason=(
                    "Blocking preflight failure(s): "
                    + ", ".join(failed_blocking)
                    + ". The campaign cannot start until these are fixed."
                ),
            )
            return
        self._set_start_enabled(enabled=True, reason="")

    @property
    def preflight(self) -> dict[str, Any] | None:
        """The last preflight payload shown, or ``None`` before the first one."""
        return self._preflight

    def _set_start_enabled(self, *, enabled: bool, reason: str) -> None:
        self.start_button.setEnabled(enabled)
        self._blocking_label.setText(reason)
        self._blocking_label.setVisible(bool(reason))

    @property
    def blocking_reason(self) -> str:
        """Why Start is disabled, or ``""`` when it is not."""
        return self._blocking_label.text()

    # ------------------------------------------------------------------
    # Launching
    # ------------------------------------------------------------------
    def launch(self, request: CampaignRequest) -> int | None:
        """Start the campaign for ``request`` and return the orchestrator pid.

        No dialog, so a test can drive it with an injected launcher.

        Returns:
            The launched process id, or ``None`` when the launcher refused.
        """
        request.output_dir.mkdir(parents=True, exist_ok=True)
        pid = self._launcher(build_run_argv(request), request.output_dir / LAUNCH_LOG_NAME)
        self._launched_pid = int(pid) if pid else None
        self._launched_request = request
        return self._launched_pid

    def _on_start(self) -> None:
        """Launch the campaign and close. Reached only when Start is enabled."""
        request = self.request()
        if request is None:
            return
        self.launch(request)
        self.accept()

    @property
    def launched_pid(self) -> int | None:
        """The pid of the campaign this dialog started, if it started one."""
        return self._launched_pid

    @property
    def launched_request(self) -> CampaignRequest | None:
        """The request this dialog launched, if it launched one."""
        return self._launched_request

    def output_dir(self) -> Path:
        """The output directory currently in the form."""
        return Path(self.output_edit.text().strip() or str(default_output_dir())).expanduser()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def done(self, result: int) -> None:
        """Close, making sure no preflight thread outlives the dialog.

        A ``QThread`` still running when its parent is destroyed aborts the
        process on Windows, *after* the work succeeded - the failure mode the
        GUI testing skill warns about.
        """
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(int(PREFLIGHT_TIMEOUT_SECONDS * 1000))
        super().done(result)


def _description_label(text: str, object_name: str) -> QLabel:
    """A wrapped, indented explanation under a radio button."""
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setContentsMargins(22, 0, 0, 8)
    return label


def _with_button(widget: QWidget, button: QPushButton) -> QWidget:
    """Put a field and its button on one row."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, stretch=1)
    layout.addWidget(button)
    return container


__all__ = [
    "LAUNCH_LOG_NAME",
    "LOCK_FILE_NAME",
    "MODE_DESCRIPTION",
    "MODE_HEADLINE",
    "PREFLIGHT_JSON_NAME",
    "QUALIFICATION_MODULE",
    "STATE_FILE_NAME",
    "STOP_REQUEST_FILE_NAME",
    "SUMMARY_MD_NAME",
    "TELEMETRY_CSV_NAME",
    "UNATTENDED_WARNING",
    "CampaignMode",
    "CampaignRequest",
    "CampaignSituation",
    "DetachedLauncher",
    "PreflightRunner",
    "PreflightWorker",
    "StressQualificationDialog",
    "build_preflight_argv",
    "build_resume_argv",
    "build_run_argv",
    "campaign_situation",
    "default_output_dir",
    "default_template_path",
    "format_bytes",
    "is_pid_alive",
    "launch_detached",
    "orchestrator_pid",
    "read_preflight",
    "run_preflight_process",
]
