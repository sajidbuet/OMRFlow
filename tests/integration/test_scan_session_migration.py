"""Migration 14 and the scan-session backfill from real schema-13 projects (0.1.1 phase 2).

The projects under ``tests/fixtures/schema13/`` were written by the schema-13
build (``main`` at ``ce3f082``, 0.1.1-alpha.0 phase 1) through its own services
- see ``make_schema13_projects.py`` and ``PROVENANCE.json``.

* Case A ``one_batch`` - one batch, attended, scored and reported.
* Case B ``two_unrelated`` - two batches, no relationship.
* Case C ``linked_rescan`` - batch 1's sheet replaced from batch 2 (newer), confirmed.
* Case D ``ambiguous_chain`` - three batches chained by confirmed rescans.
* Case E - stored keys, attendance, results and reports keep their meaning.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.database import SCHEMA_VERSION
from omr_scanner.domain.scan_sessions import BatchMembership, BatchRole
from omr_scanner.services import (
    open_project,
    project_health,
    reconciliation_store,
    scan_sessions,
    scoring_store,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema13"


def _copy(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def _raw(root: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(root / "database.sqlite")
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _batches_by_creation(root: Path) -> list[str]:
    rows = _raw(root, "SELECT batch_id FROM scan_batch ORDER BY created_at")
    return [str(row[0]) for row in rows]


HISTORY = (
    "SELECT candidate_id, set_code, status, final_score, answer_key_id, scan_id "
    "FROM candidate_result ORDER BY candidate_id",
    "SELECT key_id, set_code, revision, status, answers FROM answer_key_revision ORDER BY key_id",
    "SELECT roster_id, set_id, source_name, is_active FROM candidate_roster ORDER BY roster_id",
    "SELECT report_id, set_code, report_type, status, output_path FROM generated_report "
    "ORDER BY report_id",
    "SELECT scan_id, batch_id, status, set_code_value, identifier_value FROM batch_scan "
    "ORDER BY scan_id",
    "SELECT scan_id, state, replacement_scan_id FROM scan_rejection ORDER BY scan_id",
    "SELECT COUNT(*) FROM audit_event WHERE entity_type != 'scan_session' "
    "AND entity_type != 'scan_batch'",
)


def _history(root: Path) -> list[list[tuple[object, ...]]]:
    return [_raw(root, sql) for sql in HISTORY]


def test_the_fixtures_are_schema_13_from_the_previous_build() -> None:
    provenance = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["schema_version"] == 13
    for name in provenance["projects"]:
        assert _raw(FIXTURES / name, "SELECT MAX(version) FROM schema_migration") == [(13,)]
        columns = {row[1] for row in _raw(FIXTURES / name, "PRAGMA table_info(scan_batch)")}
        assert "scan_session_id" not in columns


class TestCaseAOneBatch:
    def test_one_batch_becomes_one_sealed_legacy_session(self, tmp_path: Path) -> None:
        root = _copy("one_batch", tmp_path)
        before = _history(root)
        with open_project(root) as session:
            database = session.database
            assert database.schema_version == SCHEMA_VERSION == 14
            sessions = scan_sessions.list_scan_sessions(database)
            assert len(sessions) == 1 and sessions[0].origin == "backfill"
            (batch,) = scan_sessions.batches_of(database, sessions[0].scan_session_id)
            assert batch.role is BatchRole.LEGACY
            assert batch.membership is BatchMembership.SEALED
            assert scan_sessions.active_scan_session(database).scan_session_id == (
                sessions[0].scan_session_id
            )
            assert scan_sessions.downstream_batch_id(database) == batch.batch_id
            report = scan_sessions.backfill_report(database)
            assert report["sessions_created"] == 1 and report["ambiguous"] == []
        # Case E: nothing historical changed meaning.
        assert _history(root) == before

    def test_results_rescore_identically_after_the_upgrade(self, tmp_path: Path) -> None:
        root = _copy("one_batch", tmp_path)
        before = {
            str(row[0]): str(row[1])
            for row in _raw(root, "SELECT candidate_id, final_score FROM candidate_result")
        }
        template = build_answer_sheet_template()
        with open_project(root) as session:
            database = session.database
            batch_id = scan_sessions.downstream_batch_id(database)
            (exam_set,) = [
                item for item in reconciliation_store.list_rosters(database) if item.is_active
            ]
            scoring_store.score_batch(database, exam_set.roster_id, batch_id, template)
            after = {
                item.candidate_id: str(item.final_score)
                for item in scoring_store.list_results(database, exam_set.roster_id, batch_id)
            }
        assert after == before

    def test_the_backfill_runs_once(self, tmp_path: Path) -> None:
        root = _copy("one_batch", tmp_path)
        with open_project(root):
            pass
        with open_project(root) as session:
            assert len(scan_sessions.list_scan_sessions(session.database)) == 1
            assert scan_sessions.backfill_legacy_batches(session.database) is None

    def test_health_is_clean_after_the_upgrade(self, tmp_path: Path) -> None:
        root = _copy("one_batch", tmp_path)
        with open_project(root) as session:
            codes = {item.code for item in project_health.full_check(session.database, root).issues}
        assert not {code for code in codes if "SESSION" in code or "SUPERSESSION" in code}


class TestCaseBUnrelatedBatches:
    def test_two_unrelated_batches_stay_in_separate_sessions(self, tmp_path: Path) -> None:
        root = _copy("two_unrelated", tmp_path)
        first, second = _batches_by_creation(root)
        with open_project(root) as session:
            database = session.database
            assert len(scan_sessions.list_scan_sessions(database)) == 2
            assert scan_sessions.session_of_batch(database, first) != (
                scan_sessions.session_of_batch(database, second)
            )
            # The newest batch's session is active, so downstream reads what
            # "latest batch" pointed at before the upgrade.
            assert scan_sessions.downstream_batch_id(database) == second


class TestCaseCConfirmedRescan:
    def test_an_unambiguous_cross_batch_rescan_shares_one_session(self, tmp_path: Path) -> None:
        root = _copy("linked_rescan", tmp_path)
        before = _history(root)
        first, second = _batches_by_creation(root)
        with open_project(root) as session:
            database = session.database
            sessions = scan_sessions.list_scan_sessions(database)
            assert len(sessions) == 1
            assert {item.batch_id for item in scan_sessions.batches_of(
                database, sessions[0].scan_session_id
            )} == {first, second}
            report = scan_sessions.backfill_report(database)
            assert report["grouped"] == [[first, second]]
            # The newer batch holds only the confirmed replacement: a rescan
            # batch. Downstream stays on the original, where it is counted once.
            assert scan_sessions.batch_info(database, second).role is BatchRole.RESCAN
            assert scan_sessions.batch_info(database, first).role is BatchRole.LEGACY
            assert scan_sessions.downstream_batch_id(database) == first
            roster = next(
                item for item in reconciliation_store.list_rosters(database) if item.is_active
            )
            entries = reconciliation_store.list_entries(database, roster.roster_id, first)
            assert {entry.candidate_id for entry in entries} >= {"32001", "32002"}
        assert _history(root) == before


class TestCaseDAmbiguousChain:
    def test_three_chained_batches_are_not_merged_and_are_reported(self, tmp_path: Path) -> None:
        root = _copy("ambiguous_chain", tmp_path)
        before = _history(root)
        with open_project(root) as session:
            database = session.database
            assert len(scan_sessions.list_scan_sessions(database)) == 3
            report = scan_sessions.backfill_report(database)
            assert len(report["ambiguous"]) == 1
            assert "3 batches are chained" in report["ambiguous"][0]
            issues = project_health.full_check(database, root).issues
            assert "SCAN_SESSION_BACKFILL_AMBIGUOUS" in {item.code for item in issues}
        assert _history(root) == before


class TestReadOnlyCompatibility:
    @pytest.mark.parametrize("name", ["one_batch", "linked_rescan"])
    def test_a_schema_13_project_opens_read_only_without_migrating(
        self, tmp_path: Path, name: str
    ) -> None:
        root = _copy(name, tmp_path)
        with open_project(root, read_only=True) as session:
            database = session.database
            assert database.schema_version == 13
            sessions = scan_sessions.list_scan_sessions(database)
            assert sessions and all(item.virtual for item in sessions)
            newest = _batches_by_creation(root)[-1]
            assert scan_sessions.downstream_batch_id(database) == newest
            from omr_scanner.services import batch_store

            assert batch_store.load_summary(database, newest) is not None
            assert scan_sessions.batch_info(database, newest).role is BatchRole.LEGACY
        assert _raw(root, "SELECT MAX(version) FROM schema_migration") == [(13,)]

    def test_an_older_build_refuses_the_upgraded_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omr_scanner.database import migrations, open_project_database
        from omr_scanner.errors import SchemaVersionError

        root = _copy("one_batch", tmp_path)
        with open_project(root):
            pass
        monkeypatch.setattr(migrations, "SCHEMA_VERSION", 13)
        with pytest.raises(SchemaVersionError) as caught:
            open_project_database(root / "database.sqlite")
        assert "newer version of OMRFlow" in caught.value.user_message
