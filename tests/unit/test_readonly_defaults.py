"""A read-only session must never write, even a first-read default (Phase 10, §42)."""

from __future__ import annotations

from pathlib import Path

from omr_scanner.database.engine import open_project_database
from omr_scanner.services import report_store, scoring_store


def test_active_policy_never_writes_on_a_read_only_database(tmp_path: Path) -> None:
    db_path = tmp_path / "database.sqlite"
    open_project_database(db_path, create=True).close()

    ro = open_project_database(db_path, read_only=True)
    try:
        policy = scoring_store.active_policy(ro)
        assert policy.revision == 1
        assert policy.policy_id == 0  # never actually inserted
    finally:
        ro.close()

    # Reopening for writing must show no row was created by the read above.
    writable = open_project_database(db_path)
    try:
        created = scoring_store.active_policy(writable)
        assert created.policy_id != 0
    finally:
        writable.close()


def test_get_layout_config_never_writes_on_a_read_only_database(tmp_path: Path) -> None:
    db_path = tmp_path / "database.sqlite"
    open_project_database(db_path, create=True).close()

    ro = open_project_database(db_path, read_only=True)
    try:
        config = report_store.get_layout_config(ro)
        assert config.config_id == 0
        assert config.set_code == ""
    finally:
        ro.close()

    writable = open_project_database(db_path)
    try:
        created = report_store.get_layout_config(writable)
        assert created.config_id != 0
    finally:
        writable.close()
