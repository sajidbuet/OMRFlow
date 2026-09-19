"""Candidate data must never reach a log line (Phase 7).

Scope:
    Every Phase 7 code path that touches candidate data, exercised with
    deliberately distinctive synthetic values, asserting that none of them
    appears in captured logging.

Why this is its own module:
    The rule is stated in ``docs/ARCHITECTURE.md`` and is a Phase 7 exit
    criterion in its own right. Scattering the assertion through the functional
    tests would mean it holds where somebody remembered it; here it is a
    checklist of the paths that handle a roster, and a new one that forgets is
    a new test that fails.

What may be logged:
    Counts, row numbers, roster ids, scan ids, batch ids, file names the
    operator chose, and the *kind* of an error. Those identify work, not people.

What may never be:
    A candidate ID, a candidate name, a marks value, or an attendance value
    tied to an identifiable candidate. A user-facing message may contain them -
    reconciliation would be impossible otherwise - which is exactly why such
    messages must not be logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from omr_scanner.database import open_project_database
from omr_scanner.database.models import BatchScan, ScanBatch
from omr_scanner.domain.reconciliation import (
    AttendanceState,
    ReconciliationReason,
)
from omr_scanner.services import reconciliation_store
from omr_scanner.services.candidate_import import (
    CandidateImportError,
    ColumnMapping,
    RosterValidation,
    preview_roster,
    read_roster,
)
from omr_scanner.services.reconciliation_store import ReconciliationError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

SECRET_ID = "SECRET-ID-123"
SECRET_NAME = "PRIVATE CANDIDATE"
SECRET_MARKS = "87.5"
OTHER_SECRET_ID = "SECRET-ID-456"
OPERATOR = "Dr. X"

SECRETS = (SECRET_ID, SECRET_NAME, SECRET_MARKS, OTHER_SECRET_ID)


def assert_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    """Fail if any synthetic candidate value reached the log."""
    captured = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [str(record.args) for record in caplog.records]
        + [caplog.text]
    )
    leaked = [secret for secret in SECRETS if secret in captured]
    assert not leaked, (
        f"candidate data reached the log: {leaked}\n"
        f"--- captured ---\n{captured}"
    )


@pytest.fixture
def capture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture everything OMRFlow logs, at every level."""
    caplog.set_level(logging.DEBUG, logger="omr_scanner")
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def roster_csv(tmp_path: Path) -> Path:
    path = tmp_path / "private_roster.csv"
    path.write_text(
        "Roll No.,Name,Total (90)\n"
        f"{SECRET_ID},{SECRET_NAME},{SECRET_MARKS}\n"
        f"{OTHER_SECRET_ID},{SECRET_NAME} TWO,ABSENT\n",
        encoding="utf-8",
    )
    return path


def validation_for(path: Path) -> RosterValidation:
    return read_roster(path)


class TestImportLogging:
    def test_previewing_logs_no_candidate_data(self, capture, roster_csv):
        found = preview_roster(roster_csv)
        assert found.data_rows == 2
        assert_no_secrets(capture)

    def test_reading_logs_only_counts(self, capture, roster_csv):
        found = read_roster(roster_csv)
        assert len(found.candidates) == 2
        assert_no_secrets(capture)
        # It does log something useful.
        assert any("Roster read" in r.getMessage() for r in capture.records)

    def test_storing_a_roster_logs_no_candidate_data(
        self, capture, database, roster_csv
    ):
        reconciliation_store.import_roster(
            database, validation_for(roster_csv), imported_by=OPERATOR
        )
        assert_no_secrets(capture)

    def test_a_duplicate_id_refusal_logs_nothing_identifying(self, capture, tmp_path):
        # The user-facing message *names* the ID so the operator can find the
        # row. That message must not be what reaches the log.
        path = tmp_path / "dupes.csv"
        path.write_text(
            "Roll No.,Name,Total (90)\n"
            f"{SECRET_ID},{SECRET_NAME},55\n"
            f"{SECRET_ID},{SECRET_NAME} AGAIN,60\n",
            encoding="utf-8",
        )
        found = read_roster(path)
        assert found.can_import is False
        assert SECRET_ID in found.issues[0].message, "the operator still sees it"
        assert_no_secrets(capture)

    def test_a_refused_import_logs_nothing_identifying(
        self, capture, database, tmp_path
    ):
        path = tmp_path / "dupes.csv"
        path.write_text(
            "Roll No.,Name,Total (90)\n"
            f"{SECRET_ID},{SECRET_NAME},55\n"
            f"{SECRET_ID},{SECRET_NAME} AGAIN,60\n",
            encoding="utf-8",
        )
        with pytest.raises(ReconciliationError):
            reconciliation_store.import_roster(database, read_roster(path))
        assert_no_secrets(capture)

    def test_a_missing_id_column_logs_nothing_identifying(self, capture, tmp_path):
        path = tmp_path / "noid.csv"
        path.write_text(
            f"Thing,Other\n{SECRET_ID},{SECRET_NAME}\n", encoding="utf-8"
        )
        with pytest.raises(CandidateImportError):
            read_roster(path)
        assert_no_secrets(capture)

    def test_a_corrupt_workbook_logs_nothing_identifying(self, capture, tmp_path):
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"not a workbook " + SECRET_ID.encode())
        with pytest.raises(CandidateImportError):
            read_roster(path)
        assert_no_secrets(capture)


class TestReconciliationLogging:
    @pytest.fixture
    def prepared(self, database, roster_csv):
        roster_id = reconciliation_store.import_roster(
            database, validation_for(roster_csv), imported_by=OPERATOR
        )
        batch_id = "b" * 32
        now = datetime.now(UTC)
        with database.session() as session:
            session.add(
                ScanBatch(
                    batch_id=batch_id,
                    created_at=now,
                    updated_at=now,
                    source_folder="/scans",
                    status="completed",
                    total_scans=3,
                )
            )
            session.flush()
            for index, identifier in enumerate(
                [SECRET_ID, SECRET_ID, "SECRET-ID-999"]
            ):
                session.add(
                    BatchScan(
                        batch_id=batch_id,
                        batch_index=index,
                        source_path=f"/scans/s{index}.png",
                        filename=f"s{index}.png",
                        status="completed",
                        identifier_value=identifier,
                        result_json="",
                    )
                )
        return roster_id, batch_id

    def test_reconciling_logs_only_counts(self, capture, database, prepared):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        assert_no_secrets(capture)
        assert any("Reconciled" in r.getMessage() for r in capture.records)

    def test_an_unknown_id_is_not_logged(self, capture, database, prepared):
        # The classic leak: `logger.error("Unknown candidate ID: %s", id)`.
        roster_id, batch_id = prepared
        counts = reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        assert counts.unknown_id == 1, "the scenario really does produce one"
        assert_no_secrets(capture)
        assert "SECRET-ID-999" not in capture.text

    def test_a_duplicate_script_is_not_logged(self, capture, database, prepared):
        roster_id, batch_id = prepared
        counts = reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        assert counts.duplicate_script == 1
        assert_no_secrets(capture)

    def test_assigning_a_script_logs_no_candidate_data(
        self, capture, database, prepared
    ):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        with database.session() as session:
            scan_id = session.scalars(
                select(BatchScan.scan_id)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.filename == "s2.png")
            ).first()
        reconciliation_store.assign_script(
            database, roster_id, batch_id, scan_id,
            candidate_id=OTHER_SECRET_ID, operator=OPERATOR,
            reason=ReconciliationReason.MISREAD_IDENTIFIER,
        )
        assert_no_secrets(capture)

    def test_setting_a_script_aside_logs_no_candidate_data(
        self, capture, database, prepared
    ):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        with database.session() as session:
            scan_id = session.scalars(
                select(BatchScan.scan_id)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.filename == "s1.png")
            ).first()
        reconciliation_store.set_script_excluded(
            database, roster_id, batch_id, scan_id, excluded=True,
            operator=OPERATOR, reason=ReconciliationReason.ACCIDENTAL_RESCAN,
        )
        assert_no_secrets(capture)

    def test_an_attendance_override_logs_no_candidate_data(
        self, capture, database, prepared
    ):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        reconciliation_store.override_attendance(
            database, roster_id, batch_id, OTHER_SECRET_ID,
            attendance=AttendanceState.PRESENT, operator=OPERATOR,
            reason=ReconciliationReason.CANDIDATE_ATTENDED,
        )
        assert_no_secrets(capture)
        # The new *state* is loggable; the person it belongs to is not.
        assert any("new_state=present" in r.getMessage() for r in capture.records)

    def test_a_refused_decision_logs_no_candidate_data(
        self, capture, database, prepared
    ):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        with pytest.raises(ReconciliationError):
            reconciliation_store.override_attendance(
                database, roster_id, batch_id, "SECRET-ID-999",
                attendance=AttendanceState.PRESENT, operator=OPERATOR,
                reason=ReconciliationReason.CANDIDATE_ATTENDED,
            )
        assert_no_secrets(capture)

    def test_dismissing_an_entry_logs_no_candidate_data(
        self, capture, database, prepared
    ):
        roster_id, batch_id = prepared
        reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        reconciliation_store.dismiss_entry(
            database, roster_id, batch_id, OTHER_SECRET_ID, operator=OPERATOR,
            reason=ReconciliationReason.SCRIPT_MISSING,
        )
        assert_no_secrets(capture)


class TestTheRuleIsNotAccidental:
    """Guards against the next person writing the obvious wrong thing."""

    def test_no_phase_7_module_formats_a_candidate_id_into_a_log_call(self):
        # A grep, deliberately: the leak this phase most has to prevent is one
        # line of well-meaning debugging, and it looks the same every time.
        import re

        root = Path(__file__).resolve().parents[2] / "src" / "omr_scanner"
        suspects = []
        pattern = re.compile(
            r"_LOGGER\.[a-z]+\([^)]*"
            r"(candidate_id|display_name|imported_value|candidate\.|\.name\b)",
            re.DOTALL,
        )
        for path in (
            root / "services" / "candidate_import.py",
            root / "services" / "reconciliation.py",
            root / "services" / "reconciliation_store.py",
            root / "gui" / "attendance" / "worker.py",
            root / "gui" / "attendance" / "page.py",
        ):
            if pattern.search(path.read_text(encoding="utf-8")):
                suspects.append(path.name)
        assert not suspects, (
            "a log call in these modules references candidate data: "
            f"{suspects}. Log counts and row numbers instead."
        )

    def test_a_scan_filename_containing_a_roll_number_is_a_known_exposure(
        self, capture, tmp_path
    ):
        """Documenting a limit, not asserting a leak Phase 7 could fix.

        The Phase 3 pipeline logs the *file name* of each scan it reads, which
        is documented policy: it is the operator's own name for their own file
        and it is the only way to tell which scan failed. But an office whose
        scans are named by roll number - which is exactly what OMRFlow's own
        rename step produces - therefore has roll numbers in its application
        log, put there by Phase 3 rather than by anything here.

        This test pins that, so the next person to read the privacy rule finds
        the exception written down instead of discovering it. The fix, if one
        is wanted, belongs in `recognition_service` and costs the diagnostic.
        """
        import logging as logging_module

        logger = logging_module.getLogger("omr_scanner.services.recognition_service")
        logger.info("Scan %s could not be loaded: %s", f"{SECRET_ID}.png", "boom")
        assert SECRET_ID in capture.text, (
            "if this now passes cleanly, the filename logging was changed and "
            "the limitation recorded in docs/reconciliation.md can be removed"
        )

    def test_the_validation_message_does_name_the_candidate(self, tmp_path):
        # The counterpart: the operator's message must stay useful. If this
        # ever fails, the fix is not to weaken the privacy rule.
        path = tmp_path / "dupes.csv"
        path.write_text(
            f"Roll No.,Name\n{SECRET_ID},A\n{SECRET_ID},B\n", encoding="utf-8"
        )
        found = read_roster(path, ColumnMapping(candidate_id=0, name=1))
        assert SECRET_ID in found.issues[0].message
