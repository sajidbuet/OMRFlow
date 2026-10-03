"""Migration 17 from a real schema-16 project written by the schema-16 build.

The fixture in ``tests/fixtures/schema16`` was written by ``main`` at
``e8200b4`` (see ``make_schema16_projects.py``): an open session whose batch
holds a clean sheet, a registration failure and an ``UNUSABLE`` fold (each
read through that build's own work unit), and a ``held`` file of a closed
session. Opening it with this build:

* read-only, it is not migrated; controls read "running, intake on"; no
  decision exists; the session snapshot still works;
* writable, it is backed up first, migrated to 17, and **nothing is
  reinterpreted**: scans, conflicts, ledger, sessions and the audit ledger are
  unchanged, every session reads running with intake on, no policy is pinned
  and no decision row is written by the migration;
* afterwards decisions are derived from the stored results on first use - the
  same answers a fresh read gives - and the held file finally has an exit.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import AuditEvent, BatchScan, IntakeFile, ReviewConflict
from omr_scanner.domain.intake import IntakeState
from omr_scanner.domain.quality_decision import DEFAULT_POLICY, QualityDecision
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.session_controls import ProcessingIntent
from omr_scanner.services import (
    intake_decisions,
    open_project,
    quality_decisions,
    session_controls,
    session_snapshot,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema16"
PROVENANCE = json.loads((FIXTURES / "PROVENANCE.json").read_text(encoding="utf-8"))
OPEN = PROVENANCE["open_session"]


def copy(tmp_path: Path) -> Path:
    target = tmp_path / "continuous"
    shutil.copytree(FIXTURES / "continuous", target)
    return target


def facts(database) -> dict[str, object]:
    with database.session() as session:
        return {
            "scans": session.execute(
                select(BatchScan.scan_id, BatchScan.status, BatchScan.outcome)
                .order_by(BatchScan.scan_id)
            ).all(),
            "conflicts": session.execute(
                select(ReviewConflict.scan_id, ReviewConflict.conflict_type, ReviewConflict.state)
                .order_by(ReviewConflict.conflict_id)
            ).all(),
            "ledger": session.execute(
                select(IntakeFile.intake_file_id, IntakeFile.state).order_by(
                    IntakeFile.intake_file_id
                )
            ).all(),
            "audits": session.scalar(select(func.count()).select_from(AuditEvent)),
        }


def test_the_fixture_really_is_schema_16_from_the_previous_build():
    assert PROVENANCE["schema_version"] == 16
    assert SCHEMA_VERSION >= 17
    with sqlite3.connect(FIXTURES / "continuous" / "database.sqlite") as connection:
        assert connection.execute("SELECT max(version) FROM schema_migration").fetchone() == (16,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "scan_quality_decision" not in tables


def test_read_only_is_not_migrated_and_reads_as_before(tmp_path):
    root = copy(tmp_path)
    with open_project(root, read_only=True) as session:
        database = session.database
        assert database.schema_version == 16
        controls = session_controls.get_controls(database, OPEN)
        assert controls.processing is ProcessingIntent.RUNNING and not controls.intake_paused
        assert quality_decisions.count_outstanding(database, OPEN) == 0
        assert quality_decisions.pinned_policy(database, OPEN) is None
        assert intake_decisions.count_pending(database, OPEN) == 0
        snapshot = session_snapshot.take_snapshot(database, OPEN)
        assert snapshot.partitions
        assert snapshot.partition.queued == snapshot.partition.processing == 0


def test_upgrade_is_backed_up_and_reinterprets_nothing(tmp_path):
    root = copy(tmp_path)
    with open_project(root, read_only=True) as session:
        before = facts(session.database)
    with open_project(root) as session:
        database = session.database
        assert database.schema_version == SCHEMA_VERSION
        backups = sorted((root / "backups").glob(f"*before-migration-16-to-{SCHEMA_VERSION}*"))
        assert backups, "the pre-migration backup must run before migration 17"
        with sqlite3.connect(next(p for p in backups if p.suffix == ".sqlite3")) as backup:
            assert backup.execute("SELECT max(version) FROM schema_migration").fetchone() == (16,)
        assert facts(database) == before
        for scan_session_id in (OPEN, PROVENANCE["closed_session"]):
            controls = session_controls.get_controls(database, scan_session_id)
            assert controls.processing is ProcessingIntent.RUNNING
            assert not controls.intake_paused and controls.paused_sources == frozenset()
            assert quality_decisions.pinned_policy(database, scan_session_id) is None
        with database.session() as db:
            assert db.scalar(select(func.count()).select_from(quality_decisions_table())) == 0


def quality_decisions_table():
    from omr_scanner.database.models import ScanQualityDecision

    return ScanQualityDecision


def test_decisions_are_derived_from_the_stored_results_on_first_use(tmp_path):
    root = copy(tmp_path)
    with open_project(root) as session:
        database = session.database
        assert quality_decisions.evaluate_stored(database, OPEN) == 3
        assert quality_decisions.evaluate_stored(database, OPEN) == 0  # idempotent
        assert quality_decisions.pinned_policy(database, OPEN) == DEFAULT_POLICY
        with database.session() as db:
            by_name = {
                name: scan_id
                for scan_id, name in db.execute(select(BatchScan.scan_id, BatchScan.filename))
            }
        sheets = PROVENANCE["sheets"]
        decisions = quality_decisions.decisions_by_scan(database, by_name.values())
        assert decisions[by_name[sheets["clean"]]].decision is QualityDecision.ACCEPT
        unregistered = decisions[by_name[sheets["unregistered"]]]
        assert unregistered.decision is QualityDecision.RESCAN_REQUIRED
        assert unregistered.suggested_reason is RejectionReason.REGISTRATION
        folded = decisions[by_name[sheets["folded"]]]
        assert folded.decision is QualityDecision.RESCAN_REQUIRED
        assert folded.suggested_reason is RejectionReason.FOLDED
        assert {item.policy_fingerprint for item in decisions.values()} == {
            DEFAULT_POLICY.fingerprint
        }
        # Two suggestions, nothing rejected: a suggestion never changes a sheet.
        assert quality_decisions.count_outstanding(database, OPEN) == 2
        snapshot = session_snapshot.take_snapshot(database, OPEN)
        assert snapshot.partitions
        assert snapshot.partition.rescan_required == 2
        assert snapshot.partition.accepted == 1


def test_the_held_file_of_the_closed_session_now_has_an_exit(tmp_path):
    root = copy(tmp_path)
    closed = PROVENANCE["closed_session"]
    with open_project(root) as session:
        database = session.database
        pending = intake_decisions.pending_decisions(database, closed)
        assert [item.intake_file_id for item in pending] == [PROVENANCE["held_intake_file"]]
        assert pending[0].state is IntakeState.HELD
        assert pending[0].options == (
            intake_decisions.FileDecision.RELEASE, intake_decisions.FileDecision.DISMISS,
        )
        # Release into the closed session is refused; dismissal is recorded.
        with pytest.raises(intake_decisions.DecisionError):
            intake_decisions.decide_file(
                database, pending[0].intake_file_id, intake_decisions.FileDecision.RELEASE,
                reviewer="op",
            )
        state = intake_decisions.decide_file(
            database, pending[0].intake_file_id, intake_decisions.FileDecision.DISMISS,
            reviewer="op", note="late copy, not part of this sitting",
        )
        assert state is IntakeState.IGNORED
        assert intake_decisions.count_pending(database, closed) == 0
