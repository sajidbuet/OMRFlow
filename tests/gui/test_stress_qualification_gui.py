"""GUI validation of the 100,000-sheet qualification launcher and monitor.

Scope:
    The whole GUI front end to the Phase 10 qualification campaign - the menu
    item, the launch/preflight dialog, and the read-only monitor.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The menu item exists in Developer / Testing and is wired.
    B     The argv the dialog builds, per mode, as pure functions.
    C     Cancel is the default button; Enter cannot start a campaign.
    D     Preflight gates Start: disk shortfall, blocking failures, pass.
    E     The single-run mode says, visibly, that it is not the
          qualification.
    F     Launching goes through the injected launcher, never a process.
    G     The monitor renders a synthetic state file and telemetry tail.
    H     The monitor's three finished verdicts read differently.
    I     The monitor's buttons are enabled only when they mean something.
    J     Stop Safely writes the sentinel the harness looks for, and
          nothing else.
    K     Closing the monitor stops only its own timer.
    L     Reopening while a campaign runs reconnects instead of relaunching.
    ===== ==========================================================

**No test in this file starts a process.** Every launcher is injected and
every test that could reach one asserts that nothing was launched - a
multi-hour campaign started by a unit test would be a genuinely bad day.

**No test opens a real modal.** The dialogs are constructed and driven
through their own non-modal methods, per ``docs/TESTING.md``; the
``_prompt_*`` methods that own the modals are never called, which is the
same boundary the rest of this suite pins.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtWidgets import QMenu

from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.stress_qualification_dialog import (
    LAUNCH_LOG_NAME,
    QUALIFICATION_MODULE,
    CampaignMode,
    CampaignRequest,
    CampaignSituation,
    StressQualificationDialog,
    build_preflight_argv,
    build_resume_argv,
    build_run_argv,
    campaign_situation,
)
from omr_scanner.gui.stress_qualification_monitor import (
    RESUME_LOG_NAME,
    STOP_REQUEST_FILE_NAME,
    StressQualificationMonitor,
    read_telemetry_tail,
    summary_text,
    verdict_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from pytestqt.qtbot import QtBot

pytestmark = pytest.mark.gui

STATE_FILE_NAME = "qualification_state.json"
LOCK_FILE_NAME = "qualification.lock"
SUMMARY_MD_NAME = "qualification_summary.md"
TELEMETRY_CSV_NAME = "telemetry.csv"

TELEMETRY_HEADER = (
    "timestamp,run_id,attempt,elapsed_seconds,committed,pending,failed,"
    "total_rows,sheets_per_second,coordinator_cpu_percent,"
    "coordinator_memory_mb,tree_cpu_percent,tree_memory_mb,process_count,"
    "database_bytes,disk_free_bytes"
)


# ----------------------------------------------------------------------
# Fixtures and builders
# ----------------------------------------------------------------------
class RecordingLauncher:
    """A launcher that records what it was asked to start, and starts nothing.

    Injected everywhere a real campaign would otherwise be launched. Every
    test that exercises a launch path asserts against ``calls``, so a
    regression that reached :func:`launch_detached` for real would show up as
    an empty ``calls`` list rather than as a nine-hour test.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: Sequence[str], log_path: Path) -> int:
        self.calls.append((list(argv), log_path))
        return self.pid


@pytest.fixture
def launcher() -> RecordingLauncher:
    return RecordingLauncher()


@pytest.fixture
def never_preflight() -> object:
    """A preflight runner that must not be called.

    The dialog's preflight runs on a ``QThread``; tests drive
    :meth:`StressQualificationDialog.apply_preflight` with a payload directly
    instead, so reaching this is a defect in the test, not in the dialog.
    """

    def runner(_argv: Sequence[str]) -> int:  # pragma: no cover - must not run
        raise AssertionError("A test ran a real preflight subprocess")

    return runner


@pytest.fixture
def dialog(
    qtbot: QtBot,
    tmp_path: Path,
    launcher: RecordingLauncher,
    never_preflight: object,
) -> StressQualificationDialog:
    """A launch dialog with no automatic preflight and no real launcher."""
    widget = StressQualificationDialog(
        output_dir=tmp_path / "campaign",
        template_path=tmp_path / "sheet.omrt",
        launcher=launcher,
        preflight_runner=never_preflight,  # type: ignore[arg-type]
        auto_preflight=False,
    )
    qtbot.addWidget(widget)
    return widget


def _preflight_payload(**overrides: Any) -> dict[str, Any]:
    """A ``preflight.json`` payload in which everything passes."""
    payload: dict[str, Any] = {
        "passed": True,
        "disk_sufficient": True,
        "runs": ["R0", "K01", "K25", "K50", "K75", "K99"],
        "estimated_runtime_seconds": 81000.0,
        "estimated_runtime_text": "22 h 30 min",
        "estimated_peak_disk_bytes": 57_000_000_000,
        "estimated_peak_disk_text": "53.1 GB",
        "available_disk_bytes": 140_000_000_000,
        "available_disk_text": "130.4 GB",
        "per_run_disk_bytes": 28_000_000_000,
        "environment": {"application_version": "0.1.0.dev0"},
        "checks": [
            {"name": "template readable", "passed": True, "detail": "ok", "blocking": True},
            {"name": "disk space", "passed": True, "detail": "plenty", "blocking": True},
            {
                "name": "no leftover worker processes",
                "passed": True,
                "detail": "none",
                "blocking": False,
            },
        ],
    }
    payload.update(overrides)
    return payload


def _write_state(output_dir: Path, **overrides: Any) -> Path:
    """Write a synthetic ``qualification_state.json`` and return its path."""
    payload: dict[str, Any] = {
        "schema": 1,
        "application_version": "0.1.0.dev0",
        "started_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T01:00:00+00:00",
        "overall_status": "running",
        "config": {
            "output_dir": str(output_dir),
            "template_path": "sheet.omrt",
            "sheets": 100000,
            "seed": 20260921,
            "checkpoints": [1, 25, 50, 75, 99],
            "mode": "full",
        },
        "stages": {
            "preflight": {"status": "passed", "detail": "22 h 30 min estimated"},
            "R0": {"status": "passed", "detail": "15 assertion(s), 0 failed"},
            "K01": {"status": "running", "detail": "K01: running"},
        },
        "notes": [],
    }
    payload.update(overrides)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / STATE_FILE_NAME
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_telemetry(output_dir: Path, rows: Sequence[str] = ()) -> Path:
    """Write a synthetic ``telemetry.csv`` and return its path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / TELEMETRY_CSV_NAME
    default = (
        "2026-09-21T01:00:00+00:00,K01,1,3600.0,41207,58793,0,100000,11.24,"
        "44.0,180.5,392.7,2480.0,9,2117812736,140000000000",
    )
    body = "\n".join(rows or default)
    path.write_text(f"{TELEMETRY_HEADER}\n{body}\n", encoding="utf-8")
    return path


def _write_dead_lock(output_dir: Path, pid: int = 999_999_999) -> Path:
    """Write a campaign lock naming a pid that cannot exist.

    A pid this large is beyond every platform's range, so
    ``is_pid_alive`` is reliably False without this test having to invent a
    process and kill it.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / LOCK_FILE_NAME
    path.write_text(json.dumps({"pid": pid, "started_at": "x"}), encoding="utf-8")
    return path


@pytest.fixture
def monitor_factory(
    qtbot: QtBot, launcher: RecordingLauncher
) -> object:
    """Build a monitor over a directory, with polling off and no launcher."""

    def build(output_dir: Path) -> StressQualificationMonitor:
        widget = StressQualificationMonitor(
            output_dir, launcher=launcher, auto_refresh=False
        )
        qtbot.addWidget(widget)
        return widget

    return build


# ----------------------------------------------------------------------
# A - the menu item
# ----------------------------------------------------------------------
def test_a_the_menu_item_lives_in_developer_testing_and_is_wired(
    qtbot: QtBot,
) -> None:
    """The campaign is reachable from the existing developer submenu.

    Asserted against the submenu rather than the whole menu bar, because a
    second top-level "Stress" menu is exactly the duplication this front end
    was required not to introduce.
    """
    window = MainWindow()
    qtbot.addWidget(window)

    action = window.run_stress_qualification_action
    assert action.objectName() == "runStressQualificationAction"
    assert "100,000" in action.text()
    assert "force-kill" in action.statusTip()

    developer_menus = [
        child
        for child in window.findChildren(QMenu)
        if "Developer" in child.title()
    ]
    assert developer_menus, "The Developer / Testing submenu is gone"
    assert any(action in menu.actions() for menu in developer_menus)
    assert hasattr(window, "open_stress_qualification_monitor")


# ----------------------------------------------------------------------
# B - the command lines
# ----------------------------------------------------------------------
def test_b_full_mode_builds_the_release_qualification_command(tmp_path: Path) -> None:
    """``full`` mode passes no ``--mode``, so the CLI's own default applies."""
    request = CampaignRequest(
        output_dir=tmp_path / "out",
        template_path=tmp_path / "sheet.omrt",
        mode=CampaignMode.FULL,
    )
    argv = build_run_argv(request, python_executable="py.exe")

    assert argv[:4] == ["py.exe", "-m", QUALIFICATION_MODULE, "run"]
    assert argv[argv.index("--output-dir") + 1] == str(tmp_path / "out")
    assert argv[argv.index("--template") + 1] == str(tmp_path / "sheet.omrt")
    assert "--restart" not in argv
    # Either absent, or present and equal to "full" - never "reference".
    if "--mode" in argv:
        assert argv[argv.index("--mode") + 1] == "full"


def test_b_reference_mode_asks_for_the_reference_run_only(tmp_path: Path) -> None:
    """The single-run mode must reach the CLI as ``--mode reference``.

    If this regressed to ``full``, picking "Single 100,000-sheet run" would
    silently start a six-run campaign lasting most of a day.
    """
    request = CampaignRequest(
        output_dir=tmp_path / "out",
        template_path=tmp_path / "sheet.omrt",
        mode=CampaignMode.REFERENCE,
    )
    argv = build_run_argv(request, python_executable="py.exe")
    assert argv[argv.index("--mode") + 1] == "reference"


def test_b_restart_is_only_passed_when_asked_for(tmp_path: Path) -> None:
    """``--restart`` discards an existing campaign, so it is never implicit."""
    base = CampaignRequest(
        output_dir=tmp_path / "out", template_path=tmp_path / "sheet.omrt"
    )
    assert "--restart" not in build_run_argv(base)
    restarting = CampaignRequest(
        output_dir=base.output_dir, template_path=base.template_path, restart=True
    )
    assert "--restart" in build_run_argv(restarting)


def test_b_preflight_and_resume_target_the_same_module(tmp_path: Path) -> None:
    """Every GUI-built command goes through the one headless tool."""
    request = CampaignRequest(
        output_dir=tmp_path / "out", template_path=tmp_path / "sheet.omrt"
    )
    preflight = build_preflight_argv(request, python_executable="py.exe")
    resume = build_resume_argv(tmp_path / "out", python_executable="py.exe")

    assert preflight[:4] == ["py.exe", "-m", QUALIFICATION_MODULE, "preflight"]
    assert resume[:4] == ["py.exe", "-m", QUALIFICATION_MODULE, "resume"]
    assert resume[resume.index("--output-dir") + 1] == str(tmp_path / "out")


def test_b_the_dialog_form_produces_the_request_it_shows(
    dialog: StressQualificationDialog, tmp_path: Path
) -> None:
    """What the form displays is what gets launched."""
    request = dialog.request()
    assert request is not None
    assert request.output_dir == tmp_path / "campaign"
    assert request.mode is CampaignMode.FULL

    dialog.reference_radio.setChecked(True)
    assert dialog.selected_mode() is CampaignMode.REFERENCE
    switched = dialog.request()
    assert switched is not None
    assert switched.mode is CampaignMode.REFERENCE


def test_b_an_incomplete_form_yields_no_request(
    dialog: StressQualificationDialog,
) -> None:
    """A blank output folder or template cannot become a command line."""
    dialog.output_edit.setText("   ")
    assert dialog.request() is None
    assert dialog.start_preflight() is False
    assert "output folder" in dialog.blocking_reason


# ----------------------------------------------------------------------
# C - Cancel is the default
# ----------------------------------------------------------------------
def test_c_cancel_is_the_default_button_so_enter_cannot_start_a_campaign(
    dialog: StressQualificationDialog,
) -> None:
    """Enter must not be able to start a multi-hour, process-killing run.

    Pinned explicitly because ``QDialogButtonBox`` makes the accept button
    the default by itself - this is a deliberate inversion that a later
    refactor of the button box would silently undo.
    """
    assert dialog.cancel_button.isDefault()
    assert not dialog.start_button.isDefault()
    assert not dialog.start_button.autoDefault()


# ----------------------------------------------------------------------
# D - the preflight gate
# ----------------------------------------------------------------------
def test_d_start_is_disabled_until_a_preflight_has_been_applied(
    dialog: StressQualificationDialog,
) -> None:
    """A campaign cannot be started against unknown conditions."""
    assert not dialog.start_button.isEnabled()
    assert dialog.preflight is None


def test_d_a_clean_preflight_enables_start_and_shows_the_estimates(
    dialog: StressQualificationDialog,
) -> None:
    """The estimates shown are the headless tool's, not the GUI's."""
    dialog.apply_preflight(_preflight_payload())

    assert dialog.start_button.isEnabled()
    assert dialog.blocking_reason == ""
    shown = dialog.estimate_label.text()
    assert "22 h 30 min" in shown
    assert "53.1 GB" in shown
    assert "130.4 GB" in shown
    assert "K99" in shown
    assert dialog.checks_table.rowCount() == 3
    # A non-blocking check that failed would read WARN, not FAIL.
    assert dialog.checks_table.item(2, 1).text() == "PASS"


def test_d_insufficient_disk_blocks_the_start_and_names_the_shortfall(
    dialog: StressQualificationDialog,
) -> None:
    """A campaign that runs out of disk after eight hours has wasted eight hours.

    The shortfall is the one number the GUI computes, by subtracting two
    values the payload already carries, so it is worth asserting on.
    """
    dialog.apply_preflight(
        _preflight_payload(
            passed=False,
            disk_sufficient=False,
            estimated_peak_disk_bytes=57_000_000_000,
            estimated_peak_disk_text="53.1 GB",
            available_disk_bytes=20_000_000_000,
            available_disk_text="18.6 GB",
            checks=[
                {
                    "name": "disk space",
                    "passed": False,
                    "detail": "not enough",
                    "blocking": True,
                }
            ],
        )
    )

    assert not dialog.start_button.isEnabled()
    reason = dialog.blocking_reason
    assert "Not enough disk space" in reason
    assert "53.1 GB" in reason
    assert "18.6 GB" in reason
    assert "34.5 GB short" in reason


def test_d_a_blocking_preflight_failure_blocks_the_start_and_names_the_check(
    dialog: StressQualificationDialog,
) -> None:
    """Start stays disabled while any blocking check fails, and says which."""
    dialog.apply_preflight(
        _preflight_payload(
            passed=False,
            checks=[
                {
                    "name": "deterministic generator",
                    "passed": False,
                    "detail": "sheet 0 rendered differently",
                    "blocking": True,
                },
                {
                    "name": "no leftover worker processes",
                    "passed": False,
                    "detail": "2 process(es)",
                    "blocking": False,
                },
            ],
        )
    )

    assert not dialog.start_button.isEnabled()
    assert "deterministic generator" in dialog.blocking_reason
    # The non-blocking one is shown as a warning and does not block.
    assert "no leftover worker processes" not in dialog.blocking_reason
    assert dialog.checks_table.item(0, 1).text() == "FAIL"
    assert dialog.checks_table.item(1, 1).text() == "WARN"


def test_d_a_preflight_that_could_not_be_read_blocks_rather_than_assumes(
    dialog: StressQualificationDialog,
) -> None:
    """No preflight means no campaign - never an optimistic default."""
    dialog._on_preflight_failed("preflight.json was not written")

    assert not dialog.start_button.isEnabled()
    assert dialog.preflight is None
    assert "not written" in dialog.blocking_reason
    assert dialog.checks_table.rowCount() == 0


# ----------------------------------------------------------------------
# E - the single-run mode is labelled honestly
# ----------------------------------------------------------------------
def test_e_the_single_run_mode_says_visibly_that_it_is_not_the_qualification(
    dialog: StressQualificationDialog,
) -> None:
    """The one claim this phase must never let anybody make by accident.

    Asserted against the text actually on screen, not against a constant, so
    that softening the wording fails the test.
    """
    label = dialog.findChild(type(dialog.estimate_label), "stressReferenceModeLabel")
    assert label is not None
    text = label.text()
    assert "NOT the release qualification" in text
    assert "no kills" in text

    full_label = dialog.findChild(type(dialog.estimate_label), "stressFullModeLabel")
    assert full_label is not None
    assert "only mode that qualifies" in full_label.text()
    # ...and the recommended one is the one selected on opening.
    assert dialog.full_radio.isChecked()


def test_e_the_unattended_warning_is_shown_without_promising_a_power_change(
    dialog: StressQualificationDialog,
) -> None:
    """The dialog warns about sleep and power; it never changes either.

    OMRFlow must not silently modify a system-wide Windows power setting, so
    the warning has to say so rather than implying it handled it.
    """
    label = dialog.findChild(type(dialog.estimate_label), "stressUnattendedWarningLabel")
    assert label is not None
    text = label.text()
    assert "sleep disabled" in text
    assert "does not change any power setting" in text
    assert "Closing OMRFlow will NOT stop the campaign" in text


# ----------------------------------------------------------------------
# F - launching
# ----------------------------------------------------------------------
def test_f_launching_goes_through_the_injected_launcher_and_records_the_request(
    dialog: StressQualificationDialog,
    launcher: RecordingLauncher,
    tmp_path: Path,
) -> None:
    """The dialog's launch seam is the only path to a process."""
    request = dialog.request()
    assert request is not None

    pid = dialog.launch(request)

    assert pid == launcher.pid
    assert dialog.launched_pid == launcher.pid
    assert dialog.launched_request == request
    assert len(launcher.calls) == 1
    argv, log_path = launcher.calls[0]
    assert argv[:4][1:] == ["-m", QUALIFICATION_MODULE, "run"]
    assert log_path == (tmp_path / "campaign") / LAUNCH_LOG_NAME
    # The output directory is created for the child, since it redirects its
    # own log into it before the harness has run at all.
    assert (tmp_path / "campaign").is_dir()


def test_f_a_launcher_that_refuses_reports_no_pid(
    qtbot: QtBot, tmp_path: Path, never_preflight: object
) -> None:
    """A failed launch must not leave the GUI claiming a campaign is running."""

    def refusing(_argv: Sequence[str], _log: Path) -> int:
        return 0

    widget = StressQualificationDialog(
        output_dir=tmp_path / "out",
        template_path=tmp_path / "sheet.omrt",
        launcher=refusing,
        preflight_runner=never_preflight,  # type: ignore[arg-type]
        auto_preflight=False,
    )
    qtbot.addWidget(widget)
    request = widget.request()
    assert request is not None
    assert widget.launch(request) is None
    assert widget.launched_pid is None


# ----------------------------------------------------------------------
# G - the monitor renders what is on disk
# ----------------------------------------------------------------------
def test_g_the_monitor_renders_the_stages_and_the_latest_telemetry(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """Everything shown comes from files the harness wrote."""
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    _write_telemetry(output_dir)

    monitor = monitor_factory(output_dir)
    state = monitor.refresh()

    assert state["exists"] and state["readable"]
    assert monitor.stage_table.rowCount() == 3
    names = {
        monitor.stage_table.item(row, 0).text()
        for row in range(monitor.stage_table.rowCount())
    }
    assert names == {"preflight", "R0", "K01"}
    statuses = {
        monitor.stage_table.item(row, 0).text(): monitor.stage_table.item(row, 1).text()
        for row in range(monitor.stage_table.rowCount())
    }
    assert statuses["R0"] == "passed"
    assert statuses["K01"] == "running"

    telemetry = monitor.telemetry_label.text()
    assert "41,207" in telemetry or "41207" in telemetry
    assert "11.2" in telemetry
    assert monitor.samples[-1]["run_id"] == "K01"


def test_g_the_monitor_survives_a_missing_or_corrupt_state_file(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """A half-written state file is a state to display, not an exception.

    The harness writes the state file atomically precisely so this read is
    safe, but the monitor polls on a timer and an exception there would
    escape into Qt's event loop.
    """
    empty = tmp_path / "nothing"
    empty.mkdir()
    monitor = monitor_factory(empty)
    assert monitor.refresh()["exists"] is False
    assert "No campaign found" in monitor.verdict_label.text()

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    other = monitor_factory(broken)
    state = other.refresh()
    assert state["exists"] is True
    assert state["readable"] is False
    assert "could not be read" in other.verdict_label.text()


def test_g_a_truncated_final_telemetry_row_is_skipped_not_guessed_at(
    tmp_path: Path,
) -> None:
    """The campaign flushes every sample, but the last can still be partial."""
    path = tmp_path / TELEMETRY_CSV_NAME
    good = (
        "2026-09-21T01:00:00+00:00,K01,1,3600.0,41207,58793,0,100000,11.24,"
        "44.0,180.5,392.7,2480.0,9,2117812736,140000000000"
    )
    truncated = "2026-09-21T01:00:05+00:00,K01,1,36"
    path.write_text(f"{TELEMETRY_HEADER}\n{good}\n{truncated}", encoding="utf-8")

    samples = read_telemetry_tail(path)
    assert len(samples) == 1
    assert samples[0]["committed"] == "41207"


# ----------------------------------------------------------------------
# H - the three finished verdicts
# ----------------------------------------------------------------------
def test_h_a_qualified_campaign_says_so_unambiguously() -> None:
    """The only wording that qualifies the release."""
    text = verdict_text(
        {"exists": True, "readable": True, "overall_status": "qualified"}
    )
    assert text.startswith("QUALIFIED")
    assert "100,000-sheet release qualification" in text


def test_h_a_reduced_scale_pass_is_never_reported_as_qualified() -> None:
    """Conflating these two is the exact claim Phase 10 must not make."""
    caveat = (
        "Every run passed. This campaign is NOT the Phase 10 release "
        "qualification: it ran 2,000 sheets per run, not 100,000."
    )
    text = verdict_text(
        {
            "exists": True,
            "readable": True,
            "overall_status": "passed_not_qualification",
            "notes": [caveat],
        }
    )
    assert "NOT the release qualification" in text
    assert "QUALIFIED." not in text
    assert "2,000 sheets per run" in text


def test_h_a_failed_campaign_names_its_failing_assertions() -> None:
    """A failure has to be legible without opening the report.

    Deliberately given a state file with **no** ``config``: the run list is
    normally derived from it, and an earlier version of
    ``failed_assertion_names`` iterated only that derived list - so a state
    file with an unreadable config reported "see the report for the details"
    while holding the details.
    """
    text = verdict_text(
        {
            "exists": True,
            "readable": True,
            "overall_status": "failed",
            "stages": {
                "K50": {
                    "status": "failed",
                    "data": {
                        "failed_assertions": [
                            "lost_committed_recognition_results",
                            "orphan_workers_after_forced_kill",
                        ]
                    },
                }
            },
        }
    )
    assert text.startswith("FAILED")
    assert "lost_committed_recognition_results" in text
    assert "orphan_workers_after_forced_kill" in text


def test_h_an_operator_stop_is_not_reported_as_a_failure() -> None:
    """Stopping deliberately between runs is neither success nor failure."""
    text = verdict_text(
        {"exists": True, "readable": True, "overall_status": "stopped"}
    )
    assert "STOPPED" in text
    assert "Nothing failed" in text
    assert "FAILED" not in text.replace("Nothing failed", "")


def test_h_a_live_campaign_reports_its_orchestrator_and_any_pending_stop() -> None:
    """While it runs, the verdict line is a progress line."""
    running = verdict_text(
        {
            "exists": True,
            "readable": True,
            "overall_status": "running",
            "orchestrator_running": True,
            "orchestrator_pid": 31280,
        }
    )
    assert "31280" in running
    assert "running" in running

    stopping = verdict_text(
        {
            "exists": True,
            "readable": True,
            "overall_status": "running",
            "orchestrator_running": True,
            "orchestrator_pid": 31280,
            "stop_requested": True,
        }
    )
    assert "stopping after the current run" in stopping


# ----------------------------------------------------------------------
# I - buttons mean something only when they do
# ----------------------------------------------------------------------
def test_i_open_report_is_enabled_only_once_the_report_exists(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """The report appears when the campaign stops, successfully or not."""
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()
    assert not monitor.open_report_button.isEnabled()
    assert monitor.open_report() is False

    (output_dir / SUMMARY_MD_NAME).write_text("# report\n", encoding="utf-8")
    monitor.refresh()
    assert monitor.open_report_button.isEnabled()


def test_i_stop_and_kill_need_a_live_orchestrator_and_resume_needs_a_dead_one(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """Each control is enabled exactly when it would do something."""
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    _write_dead_lock(output_dir)

    monitor = monitor_factory(output_dir)
    state = monitor.refresh()
    assert state["orchestrator_running"] is False
    assert not monitor.stop_button.isEnabled()
    assert not monitor.force_kill_button.isEnabled()
    assert monitor.resume_button.isEnabled()


def test_i_force_kill_against_a_dead_orchestrator_kills_nothing(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """A stale lock must not make the monitor kill an unrelated process.

    The pid in the lock file no longer exists, and pids are reused - killing
    whatever happens to hold it now would be a genuinely destructive bug.
    """
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    _write_dead_lock(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()

    assert monitor.force_kill_now() == ()


def test_i_resume_goes_through_the_injected_launcher(
    monitor_factory: Any, launcher: RecordingLauncher, tmp_path: Path
) -> None:
    """Resuming is the same headless tool, launched the same detached way."""
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()

    assert monitor.resume_campaign() == launcher.pid
    argv, log_path = launcher.calls[-1]
    assert argv[1:4] == ["-m", QUALIFICATION_MODULE, "resume"]
    assert log_path == output_dir / RESUME_LOG_NAME


# ----------------------------------------------------------------------
# J - Stop Safely writes the sentinel, and only that
# ----------------------------------------------------------------------
def test_j_stop_safely_writes_the_sentinel_the_harness_looks_for(
    monitor_factory: Any, launcher: RecordingLauncher, tmp_path: Path
) -> None:
    """The one thing this read-only window writes.

    Named identically to
    :data:`omr_scanner.evaluation.qualification.STOP_REQUEST_FILE_NAME`,
    which the campaign loop consumes between runs. A rename on either side
    would leave Stop Safely silently doing nothing, so this asserts the
    filename rather than only that a file appeared.
    """
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()

    path = monitor.request_stop_safely()

    assert path == output_dir / STOP_REQUEST_FILE_NAME
    assert path.is_file()
    assert monitor.state["stop_requested"] is True
    # Nothing was launched and nothing was killed to stop a campaign safely.
    assert launcher.calls == []


def test_j_the_harness_and_the_gui_agree_on_the_sentinel_name() -> None:
    """The GUI duplicates the constant; the duplication must stay in step.

    The GUI cannot import the evaluation layer - it pulls in SQLAlchemy and
    the database package, which ``tests/unit/test_architecture.py`` forbids -
    so the filename is deliberately duplicated. This is the test that stops
    the duplicate drifting.
    """
    from omr_scanner.evaluation import qualification

    assert STOP_REQUEST_FILE_NAME == qualification.STOP_REQUEST_FILE_NAME


def test_j_the_summary_text_is_pasteable_and_names_the_verdict(
    monitor_factory: Any, tmp_path: Path
) -> None:
    """Copy Summary produces something a person can paste into a ticket."""
    output_dir = tmp_path / "campaign"
    _write_state(output_dir, notes=["Killed 1 leftover stress run process(es)."])
    _write_telemetry(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()

    text = monitor.summary()
    assert str(output_dir) in text
    assert "R0" in text
    assert "committed 41207" in text
    assert "Killed 1 leftover" in text
    assert text == summary_text(monitor.state, monitor.samples)


# ----------------------------------------------------------------------
# K - closing the monitor
# ----------------------------------------------------------------------
def test_k_closing_the_monitor_stops_only_its_own_timer(
    monitor_factory: Any, launcher: RecordingLauncher, tmp_path: Path
) -> None:
    """Closing the monitor must never touch the campaign.

    The campaign runs as its own detached process; the monitor owns nothing
    but a ``QTimer``. If closing it ever launched, killed or wrote anything,
    an operator glancing at progress could end a nine-hour run.
    """
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    monitor = monitor_factory(output_dir)
    monitor.refresh()

    monitor.close()

    assert not monitor._timer.isActive()
    assert launcher.calls == []
    assert not (output_dir / STOP_REQUEST_FILE_NAME).exists()
    assert (output_dir / STATE_FILE_NAME).is_file()


# ----------------------------------------------------------------------
# L - reconnecting rather than starting a second campaign
# ----------------------------------------------------------------------
def test_l_a_live_campaign_is_classified_as_running_so_the_menu_reconnects(
    tmp_path: Path
) -> None:
    """Two orchestrators in one directory would overwrite each other's evidence.

    ``campaign_situation`` is what makes reopening the menu item reconnect,
    and it keys on a *live* pid rather than on the lock file's existence -
    a lock left behind by a crashed orchestrator must not look like a running
    campaign.
    """
    import os

    output_dir = tmp_path / "campaign"
    _write_state(output_dir)

    # This process is unquestionably alive, which is the point.
    (output_dir / LOCK_FILE_NAME).write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x"}), encoding="utf-8"
    )
    assert campaign_situation(output_dir) is CampaignSituation.RUNNING

    _write_dead_lock(output_dir)
    assert campaign_situation(output_dir) is CampaignSituation.RESUMABLE


def test_l_an_empty_directory_offers_the_launch_form(tmp_path: Path) -> None:
    """Nothing there means nothing to reconnect to."""
    empty = tmp_path / "fresh"
    empty.mkdir()
    assert campaign_situation(empty) is CampaignSituation.NONE


def test_l_the_monitor_opens_without_a_modal_so_a_headless_test_can_drive_it(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """The ``_prompt_*`` boundary this window already had to get right twice.

    ``open_stress_qualification_monitor`` is the method a test calls; every
    modal lives in ``_prompt_run_stress_qualification``, which a test never
    calls. A modal opened from inside the testable half would hang a headless
    run outright - the defect class this repo has hit in Phase 8, Phase 9 and
    twice since.
    """
    output_dir = tmp_path / "campaign"
    _write_state(output_dir)
    _write_telemetry(output_dir)

    window = MainWindow()
    qtbot.addWidget(window)
    monitor = window.open_stress_qualification_monitor(output_dir)

    assert isinstance(monitor, StressQualificationMonitor)
    qtbot.addWidget(monitor)
    assert monitor.state["exists"] is True
    assert monitor.stage_table.rowCount() == 3
    # Non-modal: an eight-hour campaign must not lock the operator out of the
    # rest of the application.
    assert not monitor.isModal()
    monitor.close()


def test_l_a_finished_campaign_is_not_offered_for_resuming(tmp_path: Path) -> None:
    """Every run passed; there is nothing left to continue."""
    output_dir = tmp_path / "campaign"
    _write_state(
        output_dir,
        overall_status="qualified",
        stages={
            "preflight": {"status": "passed", "detail": ""},
            **{
                run: {"status": "passed", "detail": ""}
                for run in ("R0", "K01", "K25", "K50", "K75", "K99")
            },
        },
    )
    _write_dead_lock(output_dir)
    assert campaign_situation(output_dir) is CampaignSituation.FINISHED
