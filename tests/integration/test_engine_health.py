"""Project Health for the continuous engine's invariants (revised phase 6).

Two genuine inconsistencies are reported (``CLAIM_OUTSIDE_RUNNING_BATCH``,
``INTAKE_REGISTERED_TWICE``); the normal transient state of a running engine
is not an error.
"""

from __future__ import annotations

from sqlalchemy import select, update
from tests.engine_rig import EngineRig, readable_sheets

from omr_scanner.database.models import BatchScan, ScanBatch, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits
from omr_scanner.services import project_health


def _codes(rig: EngineRig) -> dict[str, str]:
    report = project_health.full_check(rig.database, rig.project.root)
    return {issue.code: issue.level.value for issue in report.issues}


def _processed(project_session) -> EngineRig:
    rig = EngineRig(project_session)
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(readable_sheets(12)[:8])])
    engine = rig.new_engine()
    rig.make_ready()
    rig.run()
    engine.shutdown()
    return rig


def test_a_clean_run_reports_nothing(project_session):
    codes = _codes(_processed(project_session))
    assert "CLAIM_OUTSIDE_RUNNING_BATCH" not in codes
    assert "INTAKE_REGISTERED_TWICE" not in codes
    assert not [code for code, level in codes.items() if level == "error"]


def test_a_running_engine_is_not_an_error(project_session):
    rig = EngineRig(project_session)
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(readable_sheets(12)[:8])])
    engine = rig.new_engine(limits=EngineLimits(max_in_flight=4, claim_window=4))
    rig.make_ready()
    engine.form_units()
    engine.step()
    assert engine.in_flight == 4
    codes = _codes(rig)
    assert codes.get("STALE_PROCESSING_JOBS") == "warning"
    assert codes.get("BATCH_LEFT_RUNNING") == "warning"
    assert not [code for code, level in codes.items() if level == "error"]
    engine.shutdown()


def test_a_claim_outside_a_running_batch_is_reported(project_session):
    rig = _processed(project_session)
    with rig.database.session() as session:
        scan_id = session.scalars(select(BatchScan.scan_id)).first()
        session.execute(
            update(BatchScan).where(BatchScan.scan_id == scan_id)
            .values(status=ScanJobStatus.PROCESSING.value)
        )
    assert _codes(rig)["CLAIM_OUTSIDE_RUNNING_BATCH"] == "error"


def test_a_watched_file_registered_twice_is_reported(project_session):
    rig = _processed(project_session)
    with rig.database.session() as session:
        first, second = session.execute(
            select(BatchScan.scan_id, BatchScan.intake_file_id).order_by(BatchScan.scan_id).limit(2)
        ).all()
        session.execute(
            update(BatchScan).where(BatchScan.scan_id == second[0])
            .values(intake_file_id=first[1])
        )
        assert session.scalar(select(ScanBatch.source_id)) is not None
    assert _codes(rig)["INTAKE_REGISTERED_TWICE"] == "error"
