"""Migration 16 from real schema-15 projects written by the schema-15 build.

The fixtures in ``tests/fixtures/schema15`` were written by ``main`` at
``fd063f8`` (see ``make_schema15_projects.py``). Opening one with this build:

* read-only, it is not migrated and reads as before; intake reports nothing;
* writable, it is backed up first, migrated to schema 16, and **nothing is
  reinterpreted as intake work**: no source (the manual one is created on
  first use, not by the migration), no ledger row, every new link NULL; results,
  sessions, final-export status, duplicates and the audit ledger unchanged;
* afterwards the finite workflow records into the manual source on its next
  *Process All*, and phase 4's duplicate rule still applies across the upgrade.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.intake_fakes import jpeg

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import AuditEvent, BatchScan, ScanBatch
from omr_scanner.domain.intake import IntakeState
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    project_health,
    reconciliation_store,
    scan_sessions,
    scoring_store,
    session_population,
    session_scope,
)
from omr_scanner.services import intake as intake_service

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema15"
PROVENANCE = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))


def copy(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def results(database) -> list[tuple[str, str, str]]:
    rosters = list(scoring_store.scoring_rosters(database))
    roster = reconciliation_store.active_roster(database, None)
    if roster is not None and roster.roster_id not in rosters:
        rosters.append(roster.roster_id)
    key = PROVENANCE["finite_batch"]
    return sorted(
        (item.candidate_id, item.status.value, str(item.final_score))
        for roster_id in rosters
        for item in scoring_store.list_results(database, roster_id, key)
    )


def facts(database) -> dict[str, object]:
    with database.session() as session:
        scans = session.execute(
            select(
                BatchScan.scan_id, BatchScan.batch_id, BatchScan.status, BatchScan.content_sha256
            )
            .order_by(BatchScan.scan_id)
        ).all()
        audits = session.scalar(select(func.count()).select_from(AuditEvent))
    sessions = [
        (info.scan_session_id, info.state.value, info.batch_count)
        for info in scan_sessions.list_scan_sessions(database)
    ]
    effective = {
        info.scan_session_id: sorted(
            session_scope.population(database, info.scan_session_id).effective
        )
        for info in scan_sessions.list_scan_sessions(database)
    }
    return {
        "scans": [tuple(row) for row in scans],
        "audits": audits,
        "sessions": sessions,
        "effective": effective,
    }


def test_the_fixtures_really_are_schema_15_from_the_previous_build():
    assert PROVENANCE["schema_version"] == 15
    assert SCHEMA_VERSION >= 16  # 17 since revised phase 7 (additive)
    for name in PROVENANCE["projects"]:
        with sqlite3.connect(FIXTURES / name / "database.sqlite") as connection:
            version = connection.execute("SELECT max(version) FROM schema_migration").fetchone()
            assert version == (15,)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            assert "intake_file" not in tables


@pytest.mark.parametrize("name", ["finite", "duplicates"])
def test_read_only_is_not_migrated_and_reports_no_intake(name, tmp_path):
    root = copy(name, tmp_path)
    with open_project(root, read_only=True) as session:
        assert session.database.schema_version == 15
        assert intake_service.list_sources(session.database) == ()
        assert intake_service.ledger(session.database) == ()
        assert not intake_service.has_intake_schema(session.database)
        assert facts(session.database)["scans"]


@pytest.mark.parametrize("name", ["finite", "duplicates"])
def test_upgrade_is_backed_up_and_reinterprets_nothing(name, tmp_path):
    root = copy(name, tmp_path)
    with open_project(root, read_only=True) as session:
        before = facts(session.database)
        before_results = results(session.database) if name == "finite" else None
    with open_project(root) as session:
        database = session.database
        assert database.schema_version == SCHEMA_VERSION
        backups = sorted((root / "backups").glob(f"*before-migration-15-to-{SCHEMA_VERSION}*"))
        assert backups, "the pre-migration backup must run before migration 16"
        with sqlite3.connect(next(p for p in backups if p.suffix == ".sqlite3")) as backup:
            assert backup.execute("SELECT max(version) FROM schema_migration").fetchone() == (15,)
        assert intake_service.list_sources(database) == ()  # manual source: first use only
        assert intake_service.ledger(database) == ()
        with database.session() as db:
            assert db.scalar(
                select(func.count()).select_from(BatchScan).where(BatchScan.intake_file_id.is_not(None))
            ) == 0
            assert db.scalar(
                select(func.count()).select_from(ScanBatch).where(ScanBatch.source_id.is_not(None))
            ) == 0
        assert facts(database) == before
        if name == "finite":
            assert results(database) == before_results
            status = session_scope.final_export_status(database, PROVENANCE["finite_session"], "A")
            assert status.state == "current"
        codes = {item.code for item in project_health.full_check(database, root).issues}
        assert not {code for code in codes if code.startswith("INTAKE_")}
        assert "FOREIGN_KEY_VIOLATION" not in codes
        assert "SQLITE_INTEGRITY_CHECK_FAILED" not in codes


def test_the_duplicate_survives_the_upgrade_and_still_applies(tmp_path):
    root = copy("duplicates", tmp_path)
    with open_project(root) as session:
        database = session.database
        population = session_population.population(database, PROVENANCE["duplicates_first_batch"])
        with database.session() as db:
            statuses = dict(db.execute(select(BatchScan.filename, BatchScan.status)).all())
        assert statuses["d2-again.png"] == "duplicate"
        assert statuses["d1-other.png"] == "completed"  # another session: not a duplicate
        assert len(population.effective) == 3
        # A later Process All in the open session: the same bytes as d1 again.
        again = root / "s4" / "d1-third.png"
        again.parent.mkdir()
        again.write_bytes(b"schema15:d1")
        from tests.conftest import build_answer_sheet_template

        identity = batch_store.BatchIdentity.of(build_answer_sheet_template())
        batch = scan_sessions.start_batch(
            database, [again], identity=identity, started_by="op",
            scan_session_id=PROVENANCE["duplicates_open_session"],
            acknowledge_template_change=True,
        )
        assert intake_service.record_manual_batch(database, batch) == 1
        linked = intake_service.link_exact_duplicates(database, batch)
        (row,) = intake_service.ledger(database)
        assert row.state is IntakeState.DUPLICATE_CONTENT
        assert linked == [(row.intake_file_id, row.duplicate_of_scan_id)]
        manual = intake_service.list_sources(database)
        assert [(source.kind.value, source.is_builtin) for source in manual] == [("manual", True)]


def test_a_new_project_and_an_upgraded_one_have_the_same_intake_schema(tmp_path):
    root = copy("finite", tmp_path)
    with open_project(root):
        pass
    fresh = create_project(tmp_path / "fresh", "fresh")
    fresh_db = fresh.database.path
    fresh.close()

    def shape(path: Path) -> dict[str, list[tuple[str, str, int]]]:
        with sqlite3.connect(path) as connection:
            return {
                table: [
                    (row[1], row[2].upper(), row[3])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                    if table != "batch_scan" or row[1] in ("intake_file_id", "registered_at")
                ]
                for table in (
                    "intake_source", "intake_source_attachment", "intake_file", "batch_scan"
                )
            }

    upgraded, created = shape(root / "database.sqlite"), shape(fresh_db)
    assert sorted(upgraded["intake_file"]) == sorted(created["intake_file"])
    assert sorted(upgraded["intake_source"]) == sorted(created["intake_source"])
    assert sorted(upgraded["batch_scan"]) == sorted(created["batch_scan"])

    def indexes(path: Path) -> set[str]:
        with sqlite3.connect(path) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%intake%'"
                )
            }

    assert indexes(root / "database.sqlite") == indexes(fresh_db)
    assert {"uq_intake_file_current_path", "uq_intake_file_content", "ix_intake_file_hash",
            "ix_intake_file_state_ready", "ix_batch_scan_intake_file"} <= indexes(fresh_db)


def test_watched_intake_works_on_an_upgraded_project(tmp_path):
    from tests.intake_fakes import FakeClock, FakeFileSystem

    root = copy("duplicates", tmp_path)
    with open_project(root) as session:
        database = session.database
        clock = FakeClock()
        fs = FakeFileSystem(clock)
        fs.add_root("S:\\scanner")
        source = intake_service.create_source(database, label="S", root_path="S:\\scanner")
        intake_service.attach_source(
            database, source.source_id, PROVENANCE["duplicates_open_session"]
        )
        service = intake_service.IntakeService(database, root, fs=fs, clock=clock)
        fs.write("S:\\scanner", "000001.jpg", jpeg(1))
        service.reconcile(source.source_id)
        clock.advance(6)
        service.reconcile(source.source_id)
        items = service.ready_items(scan_session_id=PROVENANCE["duplicates_open_session"])
        from tests.conftest import build_answer_sheet_template

        outcome = service.register(
            scan_session_id=PROVENANCE["duplicates_open_session"], source_id=source.source_id,
            intake_file_ids=[item.intake_file_id for item in items],
            identity=batch_store.BatchIdentity.of(build_answer_sheet_template()),
            acknowledge_template_change=True,
        )
        assert len(outcome.registered) == 1
