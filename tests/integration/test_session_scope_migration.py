"""Migration 15 from real schema-14 projects written by the schema-14 build.

The fixtures in ``tests/fixtures/schema14`` were written by ``main`` at
``0e94d67`` (see ``make_schema14_projects.py``). Opening one with this build:

* read-only, it is not migrated and still reads exactly what the schema-14
  build showed;
* writable, it is backed up, migrated to schema 15, its sessions' downstream
  stores are bound - the store the schema-14 build showed, recorded with an
  audit event when earlier state is kept as history - and every result reads
  the same as before;
* an output generated before migration 15 has no recorded session, so it is
  reported as no final export rather than guessed to be current.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import AuditEvent, ScanSession
from omr_scanner.services import (
    open_project,
    project_health,
    reconciliation_store,
    report_store,
    scan_sessions,
    scoring_store,
    session_population,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema14"
PROVENANCE = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))


def copy(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def results(database) -> list[tuple[str, str, str]]:
    roster = reconciliation_store.active_roster(database, None)
    rosters = [roster.roster_id] if roster is not None else []
    rosters += [
        item for item in scoring_store.scoring_rosters(database) if item not in rosters
    ]
    key = scan_sessions.downstream_batch_id(database)
    assert key is not None
    return sorted(
        (item.candidate_id, item.status.value, str(item.final_score))
        for roster_id in rosters
        for item in scoring_store.list_results(database, roster_id, key)
    )


def test_the_fixtures_really_are_schema_14_from_the_previous_build():
    assert PROVENANCE["schema_version"] == 14
    assert SCHEMA_VERSION == 15


@pytest.mark.parametrize("name", ["one_session", "split_state"])
def test_read_only_reads_what_the_schema_14_build_showed(name, tmp_path):
    root = copy(name, tmp_path)
    with open_project(root, read_only=True) as session:
        assert session.database.schema_version == 14
        found = results(session.database)
        assert found
        if name == "split_state":
            # The schema-14 build read the newer batch; so does this one.
            assert scan_sessions.downstream_batch_id(session.database) == (
                PROVENANCE["second_batch_of_split_state"]
            )


@pytest.mark.parametrize("name", ["one_session", "split_state"])
def test_upgrading_binds_the_store_and_changes_no_result(name, tmp_path):
    root = copy(name, tmp_path)
    with open_project(root, read_only=True) as session:
        before = results(session.database)
    with open_project(root) as session:
        database = session.database
        assert database.schema_version == 15
        assert (root / "backups").is_dir() and any((root / "backups").iterdir())
        assert results(database) == before
        with database.session() as db:
            bound = db.scalars(select(ScanSession.downstream_batch_id)).all()
        assert bound == [scan_sessions.downstream_batch_id(database)]
        codes = {item.code for item in project_health.full_check(database, root).issues}
        assert "SESSION_DOWNSTREAM_SPLIT" not in codes
        assert "SESSION_STORE_NOT_IN_SESSION" not in codes
        if name == "split_state":
            assert bound == [PROVENANCE["second_batch_of_split_state"]]
            with database.session() as db:
                detail = db.scalars(
                    select(AuditEvent.detail).where(AuditEvent.action == "downstream_store_bound")
                ).one()
            assert PROVENANCE["first_batch_of_split_state"][:8] in detail
        else:
            # A report written before migration 15 recorded no session.
            assert report_store.final_export_status(
                database, scan_sessions.downstream_batch_id(database), "A"
            ).state == "none"
    with open_project(root) as again:
        assert results(again.database) == before
        assert session_population.bind_downstream_stores(again.database) == 0
