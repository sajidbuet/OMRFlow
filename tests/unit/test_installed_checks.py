"""Tests for the installed-build check tool's pure parts (revised phase 10).

The checks themselves drive the installed application and are evidence runs,
not tests; what is tested here is the part that decides: whether every row and
value an older build wrote survived a migration, and the environment that
keeps a check out of the operator's real OMRFlow profile.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from tools.release_validation import installed_checks

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema12"


def _database(path: Path, rows: list[tuple[int, str]]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migration (version INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO schema_migration VALUES (?)", [(1,), (2,)])
    connection.execute("CREATE TABLE item (item_id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO item VALUES (?, ?)", rows)
    connection.commit()
    connection.close()
    return path


def test_the_schema_version_is_the_highest_migration(tmp_path):
    assert installed_checks.schema_version(_database(tmp_path / "a.sqlite", [])) == 2
    assert installed_checks.schema_version(tmp_path / "missing.sqlite") == 0


def test_added_rows_and_columns_are_allowed(tmp_path):
    before = installed_checks.table_rows(_database(tmp_path / "a.sqlite", [(1, "x")]))
    path = _database(tmp_path / "b.sqlite", [(1, "x"), (2, "new")])
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE item ADD COLUMN extra TEXT DEFAULT 'y'")
    connection.execute("INSERT INTO schema_migration VALUES (3)")
    connection.commit()
    connection.close()
    failures, counts = installed_checks.preserved(before, installed_checks.table_rows(path))
    assert failures == []
    assert counts["item"] == {"before": 1, "after": 2}


def test_a_changed_or_removed_old_row_fails(tmp_path):
    before = installed_checks.table_rows(_database(tmp_path / "a.sqlite", [(1, "x"), (2, "y")]))
    after = installed_checks.table_rows(_database(tmp_path / "b.sqlite", [(1, "CHANGED")]))
    failures, _counts = installed_checks.preserved(before, after)
    assert any("item (1,): changed" in item for item in failures)
    assert any("item (2,): row removed" in item for item in failures)


def test_a_removed_table_or_column_fails(tmp_path):
    before = installed_checks.table_rows(_database(tmp_path / "a.sqlite", [(1, "x")]))
    path = tmp_path / "b.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migration (version INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    failures, _counts = installed_checks.preserved(before, installed_checks.table_rows(path))
    assert "table item disappeared" in failures


def test_the_committed_schema12_fixture_reads_as_schema_12(tmp_path):
    copy = tmp_path / "unique_sets"
    shutil.copytree(FIXTURES / "unique_sets", copy)
    assert installed_checks.schema_version(copy / "database.sqlite") == 12
    tables = installed_checks.table_rows(copy / "database.sqlite")
    assert tables["batch_scan"]["rows"], "the fixture holds scanned sheets"


def test_the_application_profile_is_redirected(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "C:/src")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    environment = installed_checks.app_environment(tmp_path)
    assert environment["OMRFLOW_CONFIG_DIR"] == str(tmp_path / "user-config")
    assert environment["OMRFLOW_LOG_DIR"] == str(tmp_path / "user-logs")
    assert "PYTHONPATH" not in environment and "QT_QPA_PLATFORM" not in environment
