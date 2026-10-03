r"""Build the committed schema-16 project fixture - with the schema-16 build.

Purpose:
    Migration 17 (scan-quality decisions, the pinned quality policy, persisted
    pause / stop intent) must be tested from a project the previous build
    itself wrote. This script drives the schema-16 build's own services
    (``main`` at ``e8200b4``, 0.1.1 revised phase 6 plus the global UI zoom) to
    write ``continuous/``:

    * an **open** scan session with one batch read through the schema-16 work
      unit (``record_results`` with the template, so each sheet's conflicts
      are the build's own): a clean sheet, a sheet whose registration failed,
      and a sheet whose page geometry was judged ``UNUSABLE`` (a fold) - the
      three cases the quality policy maps differently;
    * a watched intake source attached to a **second** session that was then
      closed, so a file that became ready afterwards is ``held`` - the
      operator-decision state phase 6 left without an exit.

How to regenerate (only ever with the schema-16 build)::

    $env:PYTHONPATH = "<checkout of e8200b4>\src"
    .venv\Scripts\python.exe tests\fixtures\schema16\make_schema16_projects.py `
        --old-checkout <checkout of e8200b4>

The script refuses to run against a build whose schema is not 16.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPERATOR = "Fixture Builder"


def _use_old_checkout(root: Path) -> None:
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(1, str(root))


def main() -> int:
    """Write the fixture beside this script."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--old-checkout", type=Path, required=True)
    arguments = parser.parse_args()
    _use_old_checkout(arguments.old_checkout.resolve())

    from tests.conftest import build_answer_sheet_template
    from tests.intake_fakes import jpeg
    from tests.integration.test_reject_and_rescan import make_result

    from omr_scanner import __version__
    from omr_scanner.database.migrations import SCHEMA_VERSION
    from omr_scanner.domain.intake import StabilityPolicy
    from omr_scanner.domain.scan_quality import (
        ScanQualityAssessment,
        ScanQualityIssue,
        ScanQualityIssueCode,
        ScanQualityStatus,
    )
    from omr_scanner.services import (
        batch_store,
        create_project,
        scan_recovery,
        scan_sessions,
    )
    from omr_scanner.services import intake as intake_service
    from omr_scanner.services.batch_processor import ProcessedScan
    from omr_scanner.services.recognition_models import (
        RecognitionOutcome,
        RegistrationStatus,
        ScanResult,
    )

    if SCHEMA_VERSION != 16:
        print(f"Refusing: this build writes schema {SCHEMA_VERSION}, not 16", file=sys.stderr)
        return 2

    template = build_answer_sheet_template()
    workspace = HERE / "_build"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()

    session = create_project(workspace, "continuous", exam_name="Schema 16 fixture")
    database = session.database
    scans = session.root / "scans"
    scans.mkdir()
    paths = []
    for name in ("clean.png", "unregistered.png", "folded.png"):
        path = scans / name
        path.write_bytes(f"schema16:{name}".encode())
        paths.append(path)
    open_id = scan_sessions.create_scan_session(
        database, name="Open sitting", created_by=OPERATOR
    ).scan_session_id
    batch = scan_sessions.start_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template), started_by=OPERATOR,
        scan_session_id=open_id,
    )
    batch_store.mark_queued(database, batch, paths)
    batch_store.set_batch_status(database, batch, batch_store.BatchStatus.RUNNING)
    clean = make_result(paths[0], "60001", "A", 5)
    unregistered = ScanResult(
        source_path=paths[1],
        outcome=RecognitionOutcome.REGISTRATION_FAILED,
        registration=RegistrationStatus.FAILED,
        registration_message="Registration markers could not be found.",
        error_code="INSUFFICIENT_MARKERS",
        status_codes=("ALIGNMENT_FAILED", "MARKER_NOT_FOUND"),
        scan_quality=ScanQualityAssessment(evaluated=False),
    )
    fold = ScanQualityIssue(
        code=ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION,
        status=ScanQualityStatus.UNUSABLE,
        detail="Fixture: a page-wide fold.",
    )
    folded = dataclasses.replace(
        make_result(paths[2], "60003", "A", 4),
        outcome=RecognitionOutcome.REVIEW,
        status_codes=("SCAN_QUALITY_UNUSABLE",),
        scan_quality=ScanQualityAssessment(
            status=ScanQualityStatus.UNUSABLE, issues=(fold,), evaluated=True,
            reason=fold.summary(),
        ),
    )
    batch_store.record_results(
        database, batch, [ProcessedScan(result=item) for item in (clean, unregistered, folded)],
        template=template,
    )
    scan_recovery.complete_batch_review_state(database, batch)
    batch_store.finalise_batch(database, batch)

    # A watched source served a sitting that was then closed: a file that
    # became ready afterwards is held (schema 16 has no exit from held).
    incoming = session.root / "incoming"
    incoming.mkdir()
    closed_id = scan_sessions.create_scan_session(
        database, name="Closed sitting", created_by=OPERATOR, activate=False
    ).scan_session_id
    source = intake_service.create_source(
        database, label="Scanner Z", root_path=str(incoming), created_by=OPERATOR,
        policy=StabilityPolicy(min_observations=1, quiet_seconds=0.0, poll_interval_seconds=0),
    )
    intake_service.attach_source(database, source.source_id, closed_id, actor=OPERATOR)
    scan_sessions.close_scan_session(database, closed_id, closed_by=OPERATOR)
    (incoming / "late.jpg").write_bytes(jpeg(16))
    service = intake_service.IntakeService(database, session.root)
    service.reconcile(source.source_id)
    service.reconcile(source.source_id)
    held = [row for row in intake_service.ledger(database) if row.state.value == "held"]
    assert len(held) == 1, intake_service.ledger(database)
    scan_sessions.set_active_scan_session(database, open_id, activated_by=OPERATOR)
    session.close()

    target = HERE / "continuous"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(session.root, target, ignore=shutil.ignore_patterns("backups", "logs"))
    shutil.rmtree(workspace, ignore_errors=True)
    (HERE / "PROVENANCE.json").write_text(
        json.dumps(
            {
                "written_by_version": __version__,
                "written_by_commit": "e8200b4",
                "schema_version": SCHEMA_VERSION,
                "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "projects": ["continuous"],
                "open_session": open_id,
                "closed_session": closed_id,
                "batch": batch,
                "source": source.source_id,
                "held_intake_file": held[0].intake_file_id,
                "sheets": {"clean": paths[0].name, "unregistered": paths[1].name,
                           "folded": paths[2].name},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote continuous with OMRFlow {__version__} (schema 16)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
