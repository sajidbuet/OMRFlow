"""Manual *Add Folder -> Process All* converges on the intake ledger (revised phase 5).

The Scan stage's worker records every batch it registers into the project's
built-in manual source (:func:`intake.record_manual_batch`), through the same
hash routine and the same phase 4 duplicate rule watched intake uses - so the
two cannot drift into different definitions of "the same content". These
tests drive the worker's exact two steps headlessly; ``tests/gui`` covers the
real window.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.intake_fakes import FakeClock, jpeg

from omr_scanner.database.models import AuditEvent, BatchScan, ScanBatch
from omr_scanner.domain.intake import IntakeState, SourceKind, StabilityPolicy
from omr_scanner.services import batch_store, scan_lifecycle, scan_provenance, scan_sessions
from omr_scanner.services import intake as intake_service
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.intake_fs import OsFileSystem

SERVICES = Path(__file__).resolve().parents[2] / "src" / "omr_scanner" / "services"


def process_all(database, paths, identity, *, session_id=None, role=None) -> str:
    """What *Process All* does before reading: register, record, link duplicates."""
    from omr_scanner.domain.scan_sessions import BatchRole

    batch = scan_sessions.start_batch(
        database, paths, identity=identity, started_by="op",
        scan_session_id=session_id, role=role or BatchRole.SCAN,
    )
    intake_service.record_manual_batch(database, batch)
    scan_lifecycle.link_exact_duplicates(database, batch, paths)
    intake_service.mirror_duplicates(database, batch)
    return batch


def files(folder: Path, seeds: dict[str, int]) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for name, seed in seeds.items():
        path = folder / name
        path.write_bytes(jpeg(seed))
        made.append(path)
    return made


@pytest.fixture
def identity(answer_sheet_template):
    return batch_store.BatchIdentity.of(answer_sheet_template)


def test_add_folder_records_into_the_lazily_created_manual_source(project_session, identity):
    database = project_session.database
    assert intake_service.list_sources(database) == ()
    paths = files(project_session.root / "exam", {"1.jpg": 1, "2.jpg": 2, "3.jpg": 3})
    batch = process_all(database, paths, identity)
    (manual,) = intake_service.list_sources(database)
    assert manual.kind is SourceKind.MANUAL and manual.is_builtin
    rows = intake_service.ledger(database)
    assert [row.state for row in rows] == [IntakeState.REGISTERED] * 3
    assert [row.relative_path for row in rows] == [str(path) for path in paths]
    with database.session() as session:
        scans = session.execute(
            select(BatchScan.scan_id, BatchScan.intake_file_id, BatchScan.content_sha256)
            .where(BatchScan.batch_id == batch).order_by(BatchScan.batch_index)
        ).all()
        assert session.get(ScanBatch, batch).source_id == manual.source_id
        created = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "source_created")
        ).all()
    assert len(created) == 1
    for row, (scan_id, intake_id, digest) in zip(rows, scans, strict=True):
        assert row.batch_scan_id == scan_id and intake_id == row.intake_file_id
        assert row.content_sha256 == digest == scan_provenance.hash_file(Path(row.absolute_path))
    # The scans are pending: recording is not reading.
    with database.session() as session:
        statuses = session.scalars(select(BatchScan.status).where(BatchScan.batch_id == batch))
        assert set(statuses) == {
            "pending"
        }


def test_recording_twice_changes_nothing(project_session, identity):
    database = project_session.database
    paths = files(project_session.root / "exam", {"1.jpg": 1})
    batch = process_all(database, paths, identity)
    before = intake_service.ledger(database)
    assert intake_service.record_manual_batch(database, batch) == 0
    assert intake_service.ledger(database) == before


def test_reprocess_all_links_to_the_same_ledger_rows(project_session, identity):
    database = project_session.database
    paths = files(project_session.root / "exam", {"1.jpg": 1, "2.jpg": 2})
    original = process_all(database, paths, identity)
    reprocess = scan_sessions.start_reprocess_batch(database, original, identity=identity)
    intake_service.record_manual_batch(database, reprocess)
    rows = intake_service.ledger(database)
    assert len(rows) == 2
    with database.session() as session:
        links = session.execute(
            select(BatchScan.batch_id, BatchScan.intake_file_id).order_by(BatchScan.scan_id)
        ).all()
    assert sorted(intake for batch, intake in links if batch == reprocess) == sorted(
        row.intake_file_id for row in rows
    )
    assert {row.state for row in rows} == {IntakeState.REGISTERED}


def test_same_bytes_under_another_name_is_duplicate_content(project_session, identity):
    database = project_session.database
    first = process_all(database, files(project_session.root / "a", {"x.jpg": 5}), identity)
    copies = files(project_session.root / "b", {"copy of x.jpg": 5})
    second = process_all(database, copies, identity)
    rows = {row.file_name: row for row in intake_service.ledger(database)}
    assert rows["x.jpg"].state is IntakeState.REGISTERED
    assert rows["copy of x.jpg"].state is IntakeState.DUPLICATE_CONTENT
    with database.session() as session:
        original = session.scalar(select(BatchScan.scan_id).where(BatchScan.batch_id == first))
        copy_status = session.scalar(select(BatchScan.status).where(BatchScan.batch_id == second))
    assert rows["copy of x.jpg"].duplicate_of_scan_id == original
    assert copy_status == "duplicate"


def test_the_same_folder_added_again_in_the_session(project_session, identity):
    database = project_session.database
    paths = files(project_session.root / "exam", {"1.jpg": 1})
    process_all(database, paths, identity)
    second = process_all(database, paths, identity)
    (row,) = intake_service.ledger(database)  # one observation of one file
    assert row.state is IntakeState.REGISTERED
    with database.session() as session:
        assert session.scalar(select(BatchScan.status).where(BatchScan.batch_id == second)) == (
            "duplicate"
        )


def test_new_bytes_at_a_manual_path_are_flagged_path_reused(project_session, identity):
    database = project_session.database
    (path,) = files(project_session.root / "exam", {"1.jpg": 1})
    process_all(database, [path], identity)
    path.write_bytes(jpeg(2))
    process_all(database, [path], identity)
    old, new = intake_service.ledger(database)
    assert old.state is IntakeState.REGISTERED and not old.is_current
    assert new.state is IntakeState.REGISTERED and new.path_reused and new.is_current
    assert old.content_sha256 != new.content_sha256


def test_add_folder_and_a_watched_source_record_the_same_identity(project_session, identity):
    """D9: the same file through either path - same hash, size, time, outcome."""
    database = project_session.database
    folder = project_session.root / "scanner"
    (path,) = files(folder, {"000001.jpg": 9})
    manual_session = scan_sessions.create_scan_session(database, name="Manual").scan_session_id
    process_all(database, [path], identity, session_id=manual_session)
    watched_session = scan_sessions.create_scan_session(database, name="Watched").scan_session_id
    clock = FakeClock()
    source = intake_service.create_source(
        database, label="Scanner", root_path=str(folder),
        policy=StabilityPolicy(min_observations=2, quiet_seconds=5),
    )
    intake_service.attach_source(database, source.source_id, watched_session)
    service = IntakeService(database, project_session.root, fs=OsFileSystem(), clock=clock)
    service.reconcile(source.source_id)
    clock.advance(6)
    service.reconcile(source.source_id)
    items = service.ready_items(scan_session_id=watched_session)
    service.register(
        scan_session_id=watched_session, source_id=source.source_id,
        intake_file_ids=[item.intake_file_id for item in items], identity=identity,
    )
    manual_row, watched_row = intake_service.ledger(database)

    def identity_of(row: intake_service.LedgerRow) -> tuple[object, ...]:
        return (row.file_name, row.file_size, row.mtime_ns, row.content_sha256, row.state)

    assert identity_of(manual_row) == identity_of(watched_row)
    assert manual_row.scan_session_id == manual_session
    assert watched_row.scan_session_id == watched_session


def test_manual_and_watched_duplicates_use_one_rule(project_session, identity, monkeypatch):
    """Both paths reach phase 4's link_exact_duplicates; neither has its own."""
    calls: list[str] = []
    real = scan_lifecycle.link_exact_duplicates

    def spy(database, batch_id, paths=None) -> object:
        calls.append(batch_id)
        return real(database, batch_id, paths)

    monkeypatch.setattr(scan_lifecycle, "link_exact_duplicates", spy)
    database = project_session.database
    folder = project_session.root / "scanner"
    files(folder, {"1.jpg": 1})
    session_id = scan_sessions.create_scan_session(database, name="S").scan_session_id
    clock = FakeClock()
    source = intake_service.create_source(
        database, label="S", root_path=str(folder),
        policy=StabilityPolicy(min_observations=1, quiet_seconds=0),
    )
    intake_service.attach_source(database, source.source_id, session_id)
    service = IntakeService(database, project_session.root, fs=OsFileSystem(), clock=clock)
    service.reconcile(source.source_id)
    outcome = service.register(
        scan_session_id=session_id, source_id=source.source_id,
        intake_file_ids=[item.intake_file_id for item in service.ready_items(
            scan_session_id=session_id)],
        identity=identity,
    )
    manual = process_all(database, files(project_session.root / "m", {"2.jpg": 2}), identity,
                         session_id=session_id)
    assert outcome.batch_id in calls and manual in calls


class TestOneDefinition:
    """Architecture guards: one hash routine, one duplicate linker."""

    def imports(self, name: str) -> set[str]:
        tree = ast.parse((SERVICES / name).read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    @pytest.mark.parametrize("name", ["intake.py", "intake_fs.py", "image_integrity.py"])
    def test_intake_never_hashes_on_its_own(self, name):
        assert "hashlib" not in self.imports(name), (
            f"{name} must hash through scan_provenance (hash_file / hash_bytes)"
        )

    def test_only_the_lifecycle_service_writes_duplicate_content_links(self):
        writers = [
            path.name
            for path in SERVICES.glob("*.py")
            if "LifecycleState.DUPLICATE_CONTENT.value" in path.read_text(encoding="utf-8")
        ]
        assert writers == ["scan_lifecycle.py"]

    def test_the_hash_routines_agree(self, tmp_path):
        path = tmp_path / "x.jpg"
        path.write_bytes(jpeg(3))
        assert scan_provenance.hash_file(path) == scan_provenance.hash_bytes(jpeg(3))

    def test_services_have_no_qt_watcher(self):
        for name in ("intake.py", "intake_fs.py", "image_integrity.py"):
            text = (SERVICES / name).read_text(encoding="utf-8")
            assert "QFileSystemWatcher" not in text.replace("``QFileSystemWatcher``", "")
            assert not any(module.startswith("PySide6") for module in self.imports(name))
