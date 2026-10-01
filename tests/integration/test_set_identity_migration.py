"""Migration 13 (set identity) from real schema-12 projects (phase 0.1.1-A).

The projects under ``tests/fixtures/schema12/`` were written by the schema-12
build itself (``0.1.0-alpha.2`` at ``0ed96ed``) through its own services - see
``tests/fixtures/schema12/make_schema12_projects.py`` and its
``PROVENANCE.json`` - so these tests exercise the upgrade an operator's real
project takes, not an approximation of it.

* ``unique_sets`` - sets ``10`` and ``11``, Set 10 attended, scanned,
  reconciled, keyed and scored.
* ``colliding_sets`` - sets ``A``, ``a`` and ``B`` (allowed by the old exact
  comparison), Set ``A`` scored with one sheet read ``A`` and one read ``a``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.database import SCHEMA_VERSION, open_project_database
from omr_scanner.errors import OMRScannerError, SchemaVersionError
from omr_scanner.services import (
    open_project,
    project_health,
    project_sets,
    reconciliation_store,
    report_store,
    scoring_store,
    set_identity,
)
from omr_scanner.services.review_store import effective_set_codes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema12"

PRESERVED_TABLES = (
    "project_set",
    "answer_key_revision",
    "candidate_result",
    "candidate_roster",
    "registered_candidate",
    "reconciliation_entry",
    "reconciliation_script",
    "batch_scan",
    "review_conflict",
    "audit_event",
    "report_template_association",
)


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


def _schema(root: Path) -> int:
    return int(_raw(root, "SELECT MAX(version) FROM schema_migration")[0][0])


def _snapshot(root: Path) -> dict[str, object]:
    """Everything the upgrade must leave exactly as it was."""
    found: dict[str, object] = {
        table: _raw(root, f"SELECT COUNT(*) FROM {table}")[0][0] for table in PRESERVED_TABLES
    }
    found["sets"] = _raw(
        root, "SELECT set_id, code, description, display_order FROM project_set ORDER BY set_id"
    )
    found["results"] = _raw(
        root,
        "SELECT candidate_id, set_code, status, final_score, answer_key_id, scan_id "
        "FROM candidate_result ORDER BY candidate_id",
    )
    found["keys"] = _raw(
        root,
        "SELECT key_id, set_code, revision, status, answers, verified_by "
        "FROM answer_key_revision ORDER BY key_id",
    )
    found["read"] = _raw(
        root, "SELECT scan_id, set_code_value, identifier_value FROM batch_scan ORDER BY scan_id"
    )
    return found


def test_the_fixtures_really_are_schema_12_from_the_previous_build() -> None:
    provenance = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["schema_version"] == 12
    assert provenance["written_by_version"] == "0.1.0-alpha.2"
    for name in ("unique_sets", "colliding_sets"):
        assert _schema(FIXTURES / name) == 12
        columns = {row[1] for row in _raw(FIXTURES / name, "PRAGMA table_info(project_set)")}
        assert "canonical_code" not in columns
        assert "physical_mark" not in columns


class TestUniqueSets:
    def test_it_upgrades_to_schema_13_without_losing_anything(self, tmp_path: Path) -> None:
        root = _copy("unique_sets", tmp_path)
        before = _snapshot(root)
        with open_project(root) as session:
            assert session.database.schema_version == SCHEMA_VERSION == 13
        assert _snapshot(root) == before
        rows = _raw(
            root, "SELECT code, canonical_code, physical_mark FROM project_set ORDER BY code"
        )
        assert rows == [("10", "10", ""), ("11", "11", "")]

    def test_a_backup_is_taken_before_migrating(self, tmp_path: Path) -> None:
        root = _copy("unique_sets", tmp_path)
        with open_project(root):
            pass
        backups = list((root / "backups").glob("**/*"))
        assert any("before-migration-12-to-13" in str(item) for item in backups), backups

    def test_it_reopens_and_scores_identically(self, tmp_path: Path) -> None:
        root = _copy("unique_sets", tmp_path)
        before = {
            row[0]: row[3]
            for row in _raw(
                root,
                "SELECT candidate_id, set_code, status, final_score FROM candidate_result",
            )
        }
        with open_project(root):
            pass
        template = build_answer_sheet_template()
        with open_project(root) as session:
            database = session.database
            ten = project_sets.set_by_code(database, "10")
            assert ten is not None
            roster = reconciliation_store.active_roster(database, ten.set_id)
            assert roster is not None
            (batch_id,) = {row[0] for row in _raw(root, "SELECT batch_id FROM batch_scan")}
            scoring_store.score_batch(database, roster.roster_id, str(batch_id), template)
            after = {
                item.candidate_id: str(item.final_score)
                for item in scoring_store.list_results(database, roster.roster_id, str(batch_id))
            }
        assert after == {key: str(value) for key, value in before.items()}

    def test_health_reports_no_collision(self, tmp_path: Path) -> None:
        root = _copy("unique_sets", tmp_path)
        with open_project(root) as session:
            report = project_health.full_check(session.database, root)
        assert "SET_CODE_COLLISION" not in {issue.code for issue in report.issues}

    def test_a_read_only_open_does_not_migrate_and_still_reads_its_sets(
        self, tmp_path: Path
    ) -> None:
        root = _copy("unique_sets", tmp_path)
        with open_project(root, read_only=True) as session:
            assert session.database.schema_version == 12
            codes = [item.code for item in project_sets.list_sets(session.database)]
            assert codes == ["10", "11"]
            (batch_id,) = {row[0] for row in _raw(root, "SELECT batch_id FROM batch_scan")}
            found = effective_set_codes(session.database, str(batch_id))
            values = {item.value for item in found.values()}
            assert values == {"10"}
        assert _schema(root) == 12


class TestCollidingSets:
    def test_the_collision_is_kept_and_never_merged(self, tmp_path: Path) -> None:
        root = _copy("colliding_sets", tmp_path)
        before = _snapshot(root)
        with open_project(root):
            pass
        assert _snapshot(root) == before
        rows = _raw(
            root,
            "SELECT code, canonical_code FROM project_set ORDER BY display_order, code",
        )
        # The first in the operator's order holds the identity; the other keeps
        # NULL until an operator renames it. Nothing is deleted or rewritten.
        assert rows == [("A", "A"), ("a", None), ("B", "B")]
        assert sorted(row[1] for row in before["read"]) == ["A", "a"]  # type: ignore[union-attr]

    def test_health_names_the_colliding_sets(self, tmp_path: Path) -> None:
        root = _copy("colliding_sets", tmp_path)
        with open_project(root) as session:
            report = project_health.full_check(session.database, root)
        found = [issue for issue in report.issues if issue.code == "SET_CODE_COLLISION"]
        assert len(found) == 1
        assert "'A' and 'a'" in found[0].message

    def test_set_dependent_stages_refuse_until_renamed(self, tmp_path: Path) -> None:
        root = _copy("colliding_sets", tmp_path)
        template = build_answer_sheet_template()
        with open_project(root) as session:
            database = session.database
            upper = next(item for item in project_sets.list_sets(database) if item.code == "A")
            roster = reconciliation_store.active_roster(database, upper.set_id)
            assert roster is not None
            (batch_id,) = {str(row[0]) for row in _raw(root, "SELECT batch_id FROM batch_scan")}
            key_id = int(_raw(root, "SELECT key_id FROM answer_key_revision")[0][0])

            with pytest.raises(set_identity.SetCollisionError, match="Calculate Results"):
                scoring_store.score_batch(database, roster.roster_id, batch_id, template)
            with pytest.raises(set_identity.SetCollisionError, match="Reconciliation"):
                reconciliation_store.reconcile_batch(database, roster.roster_id, batch_id)
            with pytest.raises(set_identity.SetCollisionError):
                scoring_store.verify_key(database, key_id, verified_by="Operator")
            with pytest.raises(report_store.SetGenerationRefusedError) as refused:
                report_store.resolve_set_sources(database, upper.set_id)
            assert "'A' and 'a'" in refused.value.user_message

            # Neither colliding set receives a reading of the shared code.
            identity = set_identity.load(database)
            assert identity.for_reading("a") is None
            assert identity.for_reading("B") is not None

    def test_renaming_one_resolves_it_and_the_old_reading_finds_set_a(
        self, tmp_path: Path
    ) -> None:
        root = _copy("colliding_sets", tmp_path)
        template = build_answer_sheet_template()
        with open_project(root) as session:
            database = session.database
            sets = {item.code: item for item in project_sets.list_sets(database)}
            project_sets.update_set(database, sets["a"].set_id, code="C")
            assert set_identity.collisions(database) == ()
            roster = reconciliation_store.active_roster(database, sets["A"].set_id)
            assert roster is not None
            (batch_id,) = {str(row[0]) for row in _raw(root, "SELECT batch_id FROM batch_scan")}
            reconciliation_store.reconcile_batch(database, roster.roster_id, batch_id)
            scoring_store.score_batch(database, roster.roster_id, batch_id, template)
            results = scoring_store.list_results(database, roster.roster_id, batch_id)
            # The sheet read "a" is Set A now - and is marked with Set A's key.
            assert {item.candidate_id: item.status.value for item in results} == {
                "20001": "scored",
                "20002": "scored",
            }
        assert _raw(root, "SELECT code, canonical_code FROM project_set ORDER BY code") == [
            ("A", "A"),
            ("B", "B"),
            ("C", "C"),
        ]
        # The raw readings are provenance, never rewritten.
        assert sorted(row[0] for row in _raw(root, "SELECT set_code_value FROM batch_scan")) == [
            "A",
            "a",
        ]


class TestTheMigrationItself:
    def test_running_it_twice_is_harmless(self, tmp_path: Path) -> None:
        from sqlalchemy import create_engine

        from omr_scanner.database.migrations import _migration_013_set_identity

        root = _copy("colliding_sets", tmp_path)
        engine = create_engine(f"sqlite:///{root / 'database.sqlite'}")
        try:
            for _ in range(2):
                with engine.begin() as connection:
                    _migration_013_set_identity(connection)
        finally:
            engine.dispose()
        order = "SELECT code, canonical_code FROM project_set ORDER BY display_order"
        assert _raw(root, order) == [
            ("A", "A"),
            ("a", None),
            ("B", "B"),
        ]

    def test_the_unique_index_guards_the_identity(self, tmp_path: Path) -> None:
        root = _copy("unique_sets", tmp_path)
        with open_project(root):
            pass
        connection = sqlite3.connect(root / "database.sqlite")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_set SET canonical_code = '10' WHERE code = '11'"
                )
        finally:
            connection.close()


class TestAnOlderBuildRefusesTheUpgradedProject:
    def test_a_schema_12_build_refuses_it_with_the_existing_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulated: the refusal is ``version > SCHEMA_VERSION`` in the old build too."""
        from omr_scanner.database import migrations

        root = _copy("unique_sets", tmp_path)
        with open_project(root):
            pass
        monkeypatch.setattr(migrations, "SCHEMA_VERSION", 12)
        with pytest.raises(SchemaVersionError) as caught:
            open_project_database(root / "database.sqlite")
        assert "created with a newer version of OMRFlow" in caught.value.user_message

    @pytest.mark.skipif(
        not os.environ.get("OMRFLOW_SCHEMA12_CHECKOUT"),
        reason="set OMRFLOW_SCHEMA12_CHECKOUT to a 0.1.0-alpha.2-era checkout to run",
    )
    def test_the_real_previous_build_refuses_it(self, tmp_path: Path) -> None:
        checkout = Path(os.environ["OMRFLOW_SCHEMA12_CHECKOUT"])
        root = _copy("unique_sets", tmp_path)
        with open_project(root):
            pass
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from omr_scanner.database import open_project_database\n"
            "from omr_scanner.errors import SchemaVersionError\n"
            "try:\n"
            "    open_project_database(Path(sys.argv[1]))\n"
            "except SchemaVersionError as exc:\n"
            "    print('REFUSED:', exc.user_message)\n"
        )
        environment = {**os.environ, "PYTHONPATH": str(checkout / "src")}
        completed = subprocess.run(
            [sys.executable, "-c", script, str(root / "database.sqlite")],
            capture_output=True, text=True, env=environment, check=False, timeout=120,
        )
        assert "REFUSED: This project was created with a newer version of OMRFlow" in (
            completed.stdout
        ), completed.stdout + completed.stderr


def test_service_errors_are_operator_errors() -> None:
    assert issubclass(set_identity.SetCollisionError, OMRScannerError)
