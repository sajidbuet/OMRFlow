"""The finite-mode control: the same cohort as one finite batch (revised phase 9).

``ACCEPTANCE_CRITERIA.md`` X1 and the brief's finite-mode control: the
traditional *Add Folder -> Process All* workflow, in the same build, over the
same logical examination, must reach the same recognised values, marks and
report cells as the continuous multi-source session. This module runs the
finite Scan stage's own sequence of service calls - the coordinator lease, one
batch in the active session, the manual intake ledger and content hashes, the
exact-duplicate rule, the per-sheet work unit (``BatchRecorder``), the
recognition worker pool (``process_batch``), the batch-scope review pass and
finalisation - then the same scripted operator and the same endgame (close,
reports, the late script in a second batch after a reopen, re-close, reports).

It also runs the committed golden one-batch regression
(``tests/golden_one_batch.py`` against ``tests/fixtures/golden_one_batch``),
the existing baseline for reconciliation rows and workbook cell values, when
the harness runs from a source checkout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification.config import OPERATOR


@dataclass
class FiniteEvidence:
    """What the finite control produced."""

    project: Path
    session_id: str
    batches: list[str] = field(default_factory=list)
    endgame: dict[str, Any] = field(default_factory=dict)
    final_facts: Any = None
    final_population: Any = None
    final_integrity: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    recognition_seconds: float = 0.0
    error: str = ""


def _read_batch(database: Any, template: Any, template_path: Path, paths: list[Path],
                workers: int) -> str:
    """One *Process All* run, as the finite Scan stage performs it."""
    from omr_scanner.database.models import BatchStatus
    from omr_scanner.services import (
        batch_store,
        coordinator,
        intake,
        scan_lifecycle,
        scan_recovery,
        scan_sessions,
    )
    from omr_scanner.services.batch_processor import BatchOptions, process_batch
    from omr_scanner.services.recognition_settings import RecognitionOptions

    class _Owner:
        pass

    lease = coordinator.acquire(database, coordinator.CoordinatorKind.FINITE_SCAN, owner=_Owner(),
                                label="qualification finite run")
    try:
        identity = batch_store.BatchIdentity.of(template, template_path)
        batch_id = scan_sessions.start_batch(database, paths, identity=identity)
        batch_store.mark_queued(database, batch_id, paths)
        batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, template=template, commit_when_idle=True
        )
        intake.record_manual_batch(database, batch_id)
        linked = scan_lifecycle.link_exact_duplicates(database, batch_id, paths)
        intake.mirror_duplicates(database, batch_id)
        skipped = {str(item.path) for item in linked}
        todo = [path for path in paths if str(path) not in skipped]
        options = BatchOptions(
            with_preview=False,
            recognition=RecognitionOptions(with_preview=False, keep_bubble_measurements=False),
        )
        process_batch(todo, template, options=options, on_result=recorder.record,
                      workers=workers)
        recorder.flush()
        scan_recovery.complete_batch_review_state(database, batch_id)
        batch_store.finalise_batch(database, batch_id)
        return batch_id
    finally:
        lease.release()


def run_finite(campaign: Any) -> FiniteEvidence:
    """The finite control of ``campaign``'s cohort. Raises on a harness failure."""
    from omr_scanner.evaluation.intake_qualification.inspect import read_facts
    from omr_scanner.evaluation.intake_qualification.operator import OperatorActor
    from omr_scanner.evaluation.qualification import integrity_report
    from omr_scanner.services import open_project, session_finish, session_population
    from omr_scanner.services.template_service import load_template

    from .project import create_campaign_project

    began = time.monotonic()
    root = campaign.root / "finite"
    scans = root / "scans"
    scans.mkdir(parents=True, exist_ok=True)
    plan = campaign.plan
    facts = create_campaign_project(root / "workspace", "Qualification finite", plan,
                                    campaign.template_path, root / "inputs")
    paths: list[Path] = []
    for item in sorted(plan.main_arrivals, key=lambda value: value.seq):
        path = scans / f"{item.source}_{item.name}"
        shutil.copyfile(campaign.pool[item.content].path, path)
        paths.append(path)
    evidence = FiniteEvidence(project=facts.root, session_id=facts.scan_session_id)
    session = open_project(facts.root)
    database = session.database
    try:
        template = load_template(facts.template_path)
        workers = max(1, campaign.config.workers)
        recognition_began = time.monotonic()
        evidence.batches.append(
            _read_batch(database, template, facts.template_path, paths, workers)
        )
        evidence.recognition_seconds = time.monotonic() - recognition_began
        operator = OperatorActor(database, facts.scan_session_id, plan,
                                 campaign.sha_to_content())
        end = evidence.endgame
        end["operator_final"] = operator.resolve_all()
        outcome = session_finish.finish_scan_session(database, facts.scan_session_id,
                                                     closed_by=OPERATOR)
        end["first_close"] = {"closed": outcome.closed,
                              "blockers": [[b.code.value, b.count] for b in outcome.blockers]}
        end["first_downstream"] = operator.downstream(
            phase="first_close", template=template, output_dir=root / "exports" / "first_close",
            inputs=root / "inputs", project_name="Qualification finite",
        )
        session_finish.reopen_session(database, facts.scan_session_id, reopened_by=OPERATOR,
                                      reason="a missing script was found")
        late = []
        for item in plan.late_arrivals:
            path = scans / f"{item.source}_{item.name}"
            shutil.copyfile(campaign.pool[item.content].path, path)
            late.append(path)
        if late:
            evidence.batches.append(_read_batch(database, template, facts.template_path, late, 1))
        end["operator_final_reopen"] = operator.resolve_all()
        outcome = session_finish.finish_scan_session(database, facts.scan_session_id,
                                                     closed_by=OPERATOR)
        end["second_close"] = {"closed": outcome.closed,
                               "blockers": [[b.code.value, b.count] for b in outcome.blockers]}
        end["second_downstream"] = operator.downstream(
            phase="reopen", template=template, output_dir=root / "exports" / "reopen",
            inputs=root / "inputs", project_name="Qualification finite",
        )
        end["unexpected"] = sorted(operator.stats.unexpected)
        evidence.final_facts = read_facts(database, facts.scan_session_id)
        evidence.final_population = session_population.session_population(
            database, facts.scan_session_id
        )
    finally:
        session.close()
    evidence.final_integrity = integrity_report(facts.root / "database.sqlite")
    evidence.seconds = time.monotonic() - began
    return evidence


# ----------------------------------------------------------------------
# The committed golden one-batch regression
# ----------------------------------------------------------------------
def repository_root() -> Path | None:
    """The source checkout this package runs from, if it is one."""
    candidate = Path(__file__).resolve().parents[4]
    if (candidate / "tests" / "golden_one_batch.py").is_file():
        return candidate
    return None


def run_golden(output: Path) -> dict[str, Any]:
    """Run the golden one-batch scenario on this build and compare it with its fixture.

    The comparison is the existing regression's (``tests/integration/test_golden_one_batch.py``):
    every reconciliation entry, every stored result, every cell of each set's
    Rollwise, Meritwise and Answer Key sheets.
    """
    root = repository_root()
    if root is None:
        return {"ok": False, "error": "not a source checkout: tests/golden_one_batch.py absent"}
    fixture = root / "tests" / "fixtures" / "golden_one_batch" / "golden.json"
    began = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(root / "tests" / "golden_one_batch.py"), "--repo", str(root),
         str(output)],
        cwd=str(root), capture_output=True, text=True, timeout=900, check=False,
    )
    seconds = round(time.monotonic() - began, 2)
    if completed.returncode != 0 or not output.is_file():
        return {"ok": False, "error": f"golden capture failed ({completed.returncode})",
                "stderr": completed.stderr[-2000:], "seconds": seconds}
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    actual = json.loads(output.read_text(encoding="utf-8"))
    differences = [
        key for key in ("entries", "results", "workbooks") if actual.get(key) != expected.get(key)
    ]
    cells = sum(len(row) for sheets in expected["workbooks"].values()
                for rows in sheets.values() for row in rows)
    return {
        "ok": not differences,
        "differences": differences,
        "entries": sum(len(rows) for rows in expected["entries"].values()),
        "results": sum(len(rows) for rows in expected["results"].values()),
        "cells": cells,
        "fixture": str(fixture.relative_to(root)),
        "seconds": seconds,
    }


__all__ = ["FiniteEvidence", "repository_root", "run_finite", "run_golden"]
