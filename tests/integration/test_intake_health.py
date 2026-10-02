"""Project Health on the intake ledger (0.1.1 revised phase 5).

Valid transient states report nothing; each genuine inconsistency, written
directly into the database, is reported by its own code and nothing is
repaired.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text, update
from tests.intake_fakes import header_only, jpeg
from tests.integration.test_intake_ledger import ROOT_A, Rig

from omr_scanner.database.models import BatchScan, IntakeFile, IntakeSource
from omr_scanner.services import project_health


@pytest.fixture
def rig(project_session, answer_sheet_template):
    return Rig(project_session, answer_sheet_template)


def intake_codes(rig) -> set[str]:
    report = project_health.full_check(rig.database, rig.project.root)
    return {issue.code for issue in report.issues if issue.code.startswith("INTAKE_")}


def registered(rig) -> tuple[str, int, int]:
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.settle(a)
    outcome = rig.register_all(a)
    intake_id, scan_id = outcome.registered[0]
    return a, intake_id, scan_id


def test_transient_and_settled_states_are_healthy(rig):
    a, _intake_id, _scan_id = registered(rig)
    rig.fs.write(ROOT_A, "growing.jpg", header_only(jpeg(2)))
    rig.fs.write(ROOT_A, "locked.jpg", jpeg(3))
    rig.fs.file(ROOT_A, "locked.jpg").locked = True
    rig.fs.write(ROOT_A, "same.jpg", jpeg(1))  # duplicate content
    rig.fs.write(ROOT_A, "x.tmp", b"tmp")
    rig.settle(a)
    rig.register_all(a)
    assert intake_codes(rig) == set()


def test_registered_without_scan(rig):
    _a, intake_id, _scan_id = registered(rig)
    with rig.database.session() as session:
        session.execute(text("PRAGMA foreign_keys=OFF"))
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.intake_file_id == intake_id)
            .values(batch_scan_id=None)
        )
    assert "INTAKE_REGISTERED_WITHOUT_SCAN" in intake_codes(rig)


def test_link_mismatch_and_hash_mismatch(rig):
    _a, _intake_id, scan_id = registered(rig)
    with rig.database.session() as session:
        session.execute(
            update(BatchScan)
            .where(BatchScan.scan_id == scan_id)
            .values(intake_file_id=None, content_sha256="0" * 64)
        )
    assert {"INTAKE_LINK_MISMATCH", "INTAKE_HASH_MISMATCH"} <= intake_codes(rig)


def test_link_before_registration_and_malformed_hash(rig):
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.settle(a)
    _b, _intake, scan_id = registered_second(rig)
    with rig.database.session() as session:
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.relative_path == "1.jpg")
            .values(batch_scan_id=None, content_sha256="NOT-A-HASH")
        )
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.relative_path == "2.jpg")
            .values(state="stabilizing")
        )
    codes = intake_codes(rig)
    assert "INTAKE_LINK_BEFORE_REGISTRATION" in codes
    assert "INTAKE_MALFORMED_HASH" in codes
    assert scan_id


def registered_second(rig) -> tuple[str, int, int]:
    b = rig.source(r"D:\second")
    rig.fs.write(r"D:\second", "2.jpg", jpeg(2))
    rig.settle(b)
    outcome = rig.register_all(b)
    intake_id, scan_id = outcome.registered[0]
    return b, intake_id, scan_id


def test_duplicate_without_original(rig):
    a, _intake_id, _scan_id = registered(rig)
    rig.fs.write(ROOT_A, "copy.jpg", jpeg(1))
    rig.settle(a)
    rig.register_all(a)
    with rig.database.session() as session:
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.relative_path == "copy.jpg")
            .values(duplicate_of_scan_id=None)
        )
    assert "INTAKE_DUPLICATE_WITHOUT_ORIGINAL" in intake_codes(rig)


def test_missing_project_copy(rig):
    _a, intake_id, _scan_id = registered(rig)
    row = next(r for r in rig.ledger(_a) if r.intake_file_id == intake_id)
    (rig.project.root / row.ingest_path).unlink()
    assert "INTAKE_COPY_MISSING" in intake_codes(rig)


def test_session_mismatch(rig):
    from omr_scanner.services import scan_sessions

    _a, intake_id, _scan_id = registered(rig)
    other = scan_sessions.create_scan_session(rig.database, name="Other", activate=False)
    with rig.database.session() as session:
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.intake_file_id == intake_id)
            .values(scan_session_id=other.scan_session_id)
        )
    assert "INTAKE_SESSION_MISMATCH" in intake_codes(rig)


def test_two_manual_sources(rig):
    from omr_scanner.services import intake as intake_service

    manual = intake_service.manual_source(rig.database)
    with rig.database.session() as session:
        row = session.get(IntakeSource, manual.source_id)
        assert row is not None
        session.add(
            IntakeSource(
                source_id="f" * 32, label="dup", kind="manual", root_path="", is_builtin=True,
                created_at=row.created_at, updated_at=row.updated_at,
            )
        )
    assert "INTAKE_MANUAL_SOURCE_DUPLICATED" in intake_codes(rig)


def test_health_repairs_nothing(rig):
    _a, intake_id, _scan_id = registered(rig)
    with rig.database.session() as session:
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.intake_file_id == intake_id)
            .values(content_sha256="bad")
        )
    project_health.full_check(rig.database, rig.project.root)
    with rig.database.session() as session:
        assert session.get(IntakeFile, intake_id).content_sha256 == "bad"
