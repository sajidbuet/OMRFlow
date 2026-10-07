"""The SMB qualification procedure, rehearsed end to end on local folders (``stress``).

Two PowerShell scanner writers (the script the scanner PCs run), the
coordinator under test killed once with a sheet in a worker and restarted,
one source taken away (a local folder link - simulated, NOT SMB) and
returned, then the evaluator. Every assertion that can run on one machine
must pass, and the verdict must be ``NOT SMB QUALIFICATION`` - never ``PASS``:
a rehearsal is tooling evidence only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omr_scanner.evaluation.smb_qualification import evaluate
from omr_scanner.tools import smb_qualification as cli

pytestmark = [
    pytest.mark.stress,
    pytest.mark.skipif(sys.platform != "win32", reason="the scanner writer is a Windows script"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "examples" / "templates" / "synthetic_answer_sheet.omrt"


def test_the_rehearsal_runs_every_local_assertion_and_is_never_smb(tmp_path):
    code = cli.main([
        "rehearse", "--output", str(tmp_path), "--files-per-source", "40", "--duration", "90",
        "--fast-stability", "--outage-min-seconds", "20", "--offline-files", "3",
        "--template", str(TEMPLATE),
    ])
    reports = list(tmp_path.rglob("rehearsal/report.json"))
    assert len(reports) == 1, list(tmp_path.rglob("report.json"))
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert code == 0, data.get("error")
    assert data["verdict"] == evaluate.VERDICT_NOT_SMB
    statuses = {item["name"]: item["status"] for item in data["assertions"]}
    assert list(statuses) == list(evaluate.SMB_ASSERTIONS)
    # Never SMB, and below the qualification's scale: unexercised, never passed.
    assert statuses.pop("genuine_smb_topology") == "not_exercised"
    assert statuses.pop("scale_and_workload") == "not_exercised"
    assert set(statuses.values()) == {"pass"}, statuses
    measured = data["measurements"]
    assert set(measured["listing_seconds_per_reconciliation"]) == {"A", "B"}
    assert measured["stabilisation_seconds_completion_to_ready"]["all"]["count"] > 0
    assert measured["restart"]["recovery_ok"] is True
    assert "NOT SMB" in measured["outages"][0]["mechanism"]
