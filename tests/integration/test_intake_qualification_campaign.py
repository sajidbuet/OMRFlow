"""The revised phase 9 intake qualification harness, run for real (small scales).

* ``test_a_small_campaign_with_one_real_kill`` (default suite, about two
  minutes): three scanner writer processes, partial writes, byte copies, a
  source outage, one real ``TerminateProcess`` of the coordinator with work in
  flight, the restart into the same session, the operator's endgame, closure,
  reports, the late file held, reopen, re-close - and the evaluator. It must
  pass every check it exercises, and it must **not** pass the campaign: the
  crash cases it does not exercise make it ``FAILED``, never a quiet pass.
* ``test_the_harness_self_test`` (``stress``): the CLI's ``--self-test``
  campaign - every mechanism once, the uninterrupted control and the finite
  control - must end ``ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION`` with
  every assertion, crash case and endurance case passing.
* ``test_endurance_campaign`` (``stress``): ACCEPTANCE_CRITERIA.md §5.3 at a
  few hundred files - many sealed batches from three sources, several minutes
  of random arrivals with operator decisions, *Reprocess All* mid-session,
  repeated real kills including the 1 / 25 / 50 / 75 / 99 % series, and the
  interrupted result equal to the uninterrupted control and the ground truth.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from omr_scanner.evaluation.intake_qualification import assertions, config
from omr_scanner.evaluation.intake_qualification.campaign import run_campaign

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "examples" / "templates" / "synthetic_answer_sheet.omrt"

CORE = [
    "stable_files_discovered_exactly_once",
    "no_incomplete_file_processed",
    "source_provenance_retained",
    "duplicate_content_identified",
    "independent_filenames_do_not_collide",
    "batches_finite",
    "no_accepted_image_lost",
    "no_completed_scan_rerecognised",
    "offline_arrivals_discovered",
    "conflict_counts_correct_as_population_grows",
    "rescan_relationships_survive_restart",
    "aggregate_counts_consistent",
    "session_results_match_ground_truth",
    "sqlite_integrity",
    "application_invariants",
]


def by_name(payload: dict, key: str) -> dict[str, dict]:
    return {item["name"]: item for item in payload[key]}


def describe(payload: dict) -> str:
    lines = [f"verdict {payload['verdict']}; error {payload.get('error')}"]
    for key in ("assertions", "crash_matrix", "endurance"):
        lines += [f"  {item['name']}: {item['status']} {item['failures'][:3]}"
                  for item in payload.get(key, []) if item["status"] != "pass"]
    return "\n".join(lines)


def test_a_small_campaign_with_one_real_kill(tmp_path: Path) -> None:
    # The kill must land while the scanners are still writing, or nothing can
    # arrive while the coordinator is down and `offline_arrivals_discovered`
    # rightly fails. With a 36 s timeline and the kill at 50 % that depended on
    # the machine's pace: on a loaded run (canonical gate at 5c9d501, revised
    # phase 10) recognition reached 50 % half a second after the last write.
    # A longer timeline and an earlier kill keep the scenario's intent - one
    # real kill with work in flight and arrivals while it is down - without
    # that race.
    small = replace(
        config.self_test_config(),
        candidates_per_set=26, duration_seconds=54.0, kill_percents=(30,),
        after_commit_kill_percent=0, duplicate_sync_kill_percent=0, operator_kill_percent=0,
        clean_close_percent=0, reprocess=False, checkpoint_percents=(40, 100),
        run_control=False, run_finite=False, restart_offline_files=2,
        outage_start=0.25, outage_end=0.5, stage_timeout_seconds=600.0,
    )
    root, payload = run_campaign(small, tmp_path, template_path=TEMPLATE)
    assert (root / "report.json").is_file() and (root / "report.md").is_file()
    stored = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in stored["assertions"]] == list(assertions.REQUIRED_ASSERTIONS)
    assert not payload["error"], describe(payload)
    results = by_name(payload, "assertions")
    for name in CORE:
        assert results[name]["status"] == "pass", describe(payload)
    run = payload["runs"]["interrupted"]
    kills = [item for item in run["kills"] if item["kind"] == "forced"]
    assert any(k["committed_at_kill"] > 0 and k["in_flight_at_kill"] > 0 for k in kills), kills
    for kill in kills:
        assert kill["recovery_ok"] and kill["integrity_ok"], kill
        # Workers dying with the coordinator is the Windows Job Object
        # guarantee (process_containment; POSIX deliberately not implemented).
        assert sys.platform != "win32" or not kill["orphans"], kill
    # Skipping cases is never a pass: the finite control was not run and the
    # pause-point crash cases were not exercised.
    assert results["finite_mode_regression"]["status"] != "pass"
    crash = by_name(payload, "crash_matrix")
    assert crash["case_04_kill_after_commit"]["status"] == "not_exercised"
    assert payload["verdict"] == config.VERDICT_FAILED


@pytest.mark.stress
def test_the_harness_self_test(tmp_path: Path) -> None:
    root, payload = run_campaign(config.self_test_config(), tmp_path, template_path=TEMPLATE)
    assert payload["verdict"] == config.VERDICT_SMALL, describe(payload)
    for key in ("assertions", "crash_matrix", "endurance"):
        assert all(item["status"] == "pass" for item in payload[key]), describe(payload)
    assert payload["golden"]["ok"]
    assert (root / "report.md").read_text(encoding="utf-8").count("| PASS |") >= 16


@pytest.mark.stress
def test_endurance_campaign(tmp_path: Path) -> None:
    endurance = replace(
        config.self_test_config(),
        mode=config.Mode.CUSTOM, candidates_per_set=150, duration_seconds=300.0,
        control_duration_seconds=120.0, workers=4, unit_size=25, trickle_seconds=6.0,
        kill_percents=config.REQUIRED_KILL_PERCENTS, checkpoint_percents=(5, 25, 50, 75, 100),
        stage_timeout_seconds=1_200.0, restart_offline_files=5,
    )
    _root, payload = run_campaign(endurance, tmp_path, template_path=TEMPLATE)
    assert payload["verdict"] == config.VERDICT_SMALL, describe(payload)
    cases = by_name(payload, "endurance")
    for case_id, _title in assertions.ENDURANCE_CASES:
        assert cases[case_id]["status"] == "pass", describe(payload)
    assert cases["endurance_a_many_finite_batches"]["checked"] >= 10
    crash = by_name(payload, "crash_matrix")
    assert crash["case_11_resume_percentages"]["status"] == "pass"
    forced = [k for k in payload["runs"]["interrupted"]["kills"]
              if k["kind"] == "forced" and k["in_flight_at_kill"] > 0]
    points = {int(k["label"].split("@")[1].split("%")[0]) for k in forced}
    assert points == {1, 25, 50, 75, 99}
