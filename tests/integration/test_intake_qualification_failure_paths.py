"""The intake qualification harness on its failure paths.

A qualification harness that passes when something went wrong is worse than
none. These tests make the supervisor meet a crashed scanner writer, a
coordinator that never starts, a stage that hangs, a planned kill point that
is never reached, an operator interrupt and a broken evaluator - and require an
honest ``FAILED`` with a report and diagnostics every time. (A corrupt
database, a failing health check, a planted assertion failure, a missing
assertion, stale evidence of another campaign and a malformed configuration
are covered in ``tests/unit/test_intake_qualification.py``.)
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omr_scanner.evaluation.intake_qualification import (
    assertions,
    campaign,
    cohort,
    config,
    supervisor,
)
from omr_scanner.evaluation.intake_qualification.supervisor import CampaignError, ContinuousRun
from omr_scanner.services.template_service import load_template

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "examples" / "templates" / "synthetic_answer_sheet.omrt"


def stub_run(tmp_path: Path) -> ContinuousRun:
    """A run with its folders and evidence logs, and no project or child process."""
    shell = SimpleNamespace(campaign_id="p9-failure-paths", root=tmp_path,
                            config=config.self_test_config(), plan=None)
    return ContinuousRun(shell, "interrupted", SimpleNamespace(main_arrivals=[]),  # type: ignore[arg-type]
                         interrupted=True)


def python(*code: str, **kwargs: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, *code], env=supervisor.python_env(), **kwargs)


class TestTheSupervisorStopsHonestly:
    def test_a_scanner_writer_that_crashes_fails_the_run(self, tmp_path):
        run = stub_run(tmp_path)
        schedule = tmp_path / "schedule_A.json"
        schedule.write_text("{not a schedule", encoding="utf-8")  # a malformed manifest
        run.writers["A"] = python("-m", "omr_scanner.evaluation.intake_qualification.writer",
                                  str(schedule), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        run.writers["A"].wait(timeout=120)
        with pytest.raises(CampaignError, match="scanner writer A exited with code") as raised:
            run.wait_for(lambda: False, what="caught up", timeout=60)
        assert raised.value.stage == "writers"
        assert raised.value.diagnostics["code"] != 0

    def test_a_coordinator_that_fails_to_start_fails_the_run(self, tmp_path):
        run = stub_run(tmp_path)
        spec = tmp_path / "run_001.json"
        spec.write_text("{truncated", encoding="utf-8")
        run.incarnation = 1
        run.process = python("-m", "omr_scanner.evaluation.intake_qualification.coordinator",
                             "--run", str(spec), "--incarnation", "1",
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with pytest.raises(CampaignError, match=r"the coordinator exited \(1\) while waiting "
                                                r"for coordinator 1 start") as raised:
            run.wait_started()
        assert raised.value.diagnostics["coordinator_exit"] == 1

    def test_a_tail_kill_waits_for_its_target_before_its_deferral_runs(self, tmp_path):
        """A tail kill's deferral starts at its target, not when it is armed.

        Found by the first full release run: the 99 % pause, armed at 96 %,
        expired at 97 % because its 180 s counted from arming.
        """
        run = stub_run(tmp_path)
        armed = {"boundary": "in_flight_commit", "since": 0.0,
                 "trigger": {"min_committed": 9_889}}
        limit = run.DEFER_SECONDS
        assert not run.deferral_expired(armed, 9_703, limit + 1)  # still on its way
        assert not run.deferral_expired(armed, 9_889, limit + 2)  # reached: the wait starts
        assert run.deferral_expired(armed, 9_900, 2 * limit + 2)
        other = {"boundary": "after_commit", "since": 0.0, "trigger": {}}
        assert run.deferral_expired(other, 0, limit + 1)

    def test_a_hung_stage_times_out_with_diagnostics(self, tmp_path):
        run = stub_run(tmp_path)
        run.incarnation = 2
        run.process = python("-c", "import time; time.sleep(120)")
        try:
            with pytest.raises(CampaignError, match="timed out after 1s waiting for "
                                                    "the session to catch up") as raised:
                run.wait_for(lambda: None, what="the session to catch up", timeout=1.0)
            info = raised.value.diagnostics
            assert info["incarnation"] == 2 and info["coordinator_exit"] is None
            assert {"last_coordinator_events", "writers", "checkpoints"} <= set(info)
            assert raised.value.stage == "the session to catch up"
        finally:
            run.abort()
        assert run.process.poll() is not None, "abort must stop the coordinator"


# ----------------------------------------------------------------------
# The whole campaign, failing at each stage
# ----------------------------------------------------------------------
SMALL = replace(
    config.self_test_config(), candidates_per_set=26, duration_seconds=36.0,
    run_control=False, run_finite=False,
)


def fail_with(exc: BaseException) -> Any:
    def execute(self: ContinuousRun) -> Any:
        raise exc
    return execute


@pytest.fixture
def aborted(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(ContinuousRun, "abort", lambda self: calls.append(self.name))
    return calls


@pytest.mark.parametrize(("raised", "stage"), [
    (CampaignError("kill@50%: the coordinator never paused", stage="kill@50%"), "kill@50%"),
    (KeyboardInterrupt(), "interrupted by the operator"),
    (RuntimeError("a harness defect"), "harness"),
])
def test_a_failing_stage_ends_failed_with_a_full_report(monkeypatch, aborted, tmp_path,
                                                        raised, stage):
    monkeypatch.setattr(ContinuousRun, "execute", fail_with(raised))
    root, payload = campaign.run_campaign(SMALL, tmp_path, template_path=TEMPLATE)
    assert payload["verdict"] == config.VERDICT_FAILED
    assert payload["error"]["stage"] == stage
    assert aborted == ["interrupted"]
    stored = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert stored["verdict"] == config.VERDICT_FAILED
    assert [item["name"] for item in stored["assertions"]] == list(assertions.REQUIRED_ASSERTIONS)
    assert all(item["status"] != "pass" for item in stored["assertions"])
    assert config.VERDICT_FAILED in (root / "report.md").read_text(encoding="utf-8")


def test_a_broken_evaluator_still_leaves_a_failed_report(monkeypatch, aborted, tmp_path):
    monkeypatch.setattr(ContinuousRun, "execute", fail_with(CampaignError("x", stage="drive")))
    real = campaign.evaluate
    calls: list[int] = []

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise ZeroDivisionError("evaluator defect")
        return real(*args, **kwargs)

    monkeypatch.setattr(campaign, "evaluate", evaluate)
    root, payload = campaign.run_campaign(SMALL, tmp_path, template_path=TEMPLATE)
    assert len(calls) == 2
    assert payload["verdict"] == config.VERDICT_FAILED
    assert payload["error"]["stage"] == "drive"  # the first error is the one reported
    assert (root / "report.json").is_file() and (root / "report.md").is_file()


def test_a_planned_kill_point_never_reached_fails_the_release_campaign():
    """A release run whose 99 % kill never landed on work in flight cannot pass case 11."""
    def kill(label: str, in_flight: int) -> Any:
        return SimpleNamespace(kind="forced", label=label, processing_after=in_flight,
                               committed_after=10, pre={"committed": 10, "processing": 2},
                               integrity={}, incarnation=1, trigger={},
                               restart={"ok": True, "returned": in_flight, "failures": [],
                                        "down": {"processing_ids": []}})

    run = SimpleNamespace(
        kills=[kill("kill@1%", 2), kill("kill@25%", 2), kill("kill@50%", 3),
               kill("kill@75%", 2), kill("kill@99%", 0)],
        final_facts=SimpleNamespace(sheets={}, batches={}, sessions=[("s", "open", "S")],
                                    superseded_batches=set(), conflicts=[], audit=[]),
        plan_mode=config.Mode.RELEASE.value, reprocess=None, coordinator_events=[],
    )
    plan = cohort.plan_campaign(config.self_test_config(), load_template(TEMPLATE))
    index = SimpleNamespace(scan_content={}, claims_by_incarnation={})

    def case_11() -> Any:
        cases = {item.name: item for item in
                 assertions.crash_matrix(plan, run, None, index, {})}  # type: ignore[arg-type]
        return cases["case_11_resume_percentages"]

    case = case_11()
    assert case.status != "pass"
    assert any("kill@99%" in failure for failure in case.failures)
    assert any("missing: [99]" in failure for failure in case.failures)
    run.kills[-1] = kill("kill@99%", 1)
    assert not any("99" in failure for failure in case_11().failures)
