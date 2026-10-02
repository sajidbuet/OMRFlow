"""The intake ledger on a fake filesystem and a fake clock (0.1.1 revised phase 5).

Every scenario here runs against a real project database and the real intake
service; only the filesystem and the clock are fakes
(:mod:`tests.intake_fakes`), so stabilisation is exercised edge by edge
without sleeping. The same behaviour on real directories and real processes is
in ``test_intake_filesystem.py`` and ``test_intake_processes.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.intake_fakes import (
    FakeClock,
    FakeFileSystem,
    header_only,
    jpeg,
    multipage_tiff,
    png,
    random_bytes,
    tiff,
)

from omr_scanner.database.models import AuditEvent, BatchScan, IntakeFile, ScanBatch
from omr_scanner.domain.intake import (
    Exclusions,
    IntakeReason,
    IntakeState,
    Reachability,
    StabilityPolicy,
)
from omr_scanner.services import batch_store, scan_sessions
from omr_scanner.services import intake as intake_service
from omr_scanner.services.intake import IntakeError, IntakeService
from omr_scanner.services.intake_fs import IngestStore

POLICY = StabilityPolicy(
    min_observations=2, quiet_seconds=5, max_decode_attempts=3, retry_backoff_seconds=5
)
ROOT_A = r"\\scanner-a\scans"
ROOT_B = r"D:\exam\Scanner B"
ROOT_C = r"E:\ছবি\scanner c"


class Rig:
    """One project, one open session, fake time and disk, helpers."""

    def __init__(self, project_session, template) -> None:
        self.project = project_session
        self.database = project_session.database
        self.clock = FakeClock()
        self.fs = FakeFileSystem(self.clock)
        self.template = template
        self.identity = batch_store.BatchIdentity.of(template)
        self.session_id = scan_sessions.create_scan_session(
            self.database, name="Exam", created_by="op"
        ).scan_session_id
        self.service = self.restart()

    def restart(self, *, store: IngestStore | None = None) -> IntakeService:
        self.service = IntakeService(
            self.database, self.project.root, fs=self.fs, clock=self.clock, store=store
        )
        return self.service

    def source(
        self, root: str, label: str = "", *, attach: bool = True, **options: object
    ) -> str:
        self.fs.add_root(root)
        options.setdefault("policy", POLICY)
        info = intake_service.create_source(
            self.database, label=label or root, root_path=root, created_by="op",
            clock=self.clock, **options,
        )
        if attach:
            intake_service.attach_source(
                self.database, info.source_id, self.session_id, actor="op", clock=self.clock
            )
        return info.source_id

    def poll(self, *source_ids: str, advance: float = 0.0) -> list[intake_service.ReconcileReport]:
        if advance:
            self.clock.advance(advance)
        return [self.service.reconcile(source_id) for source_id in source_ids]

    def settle(self, *source_ids: str) -> None:
        """Observe twice over the quiet period: enough for a complete file to be ready."""
        self.poll(*source_ids)
        self.poll(*source_ids, advance=POLICY.quiet_seconds + 0.1)

    def row(self, source_id: str, relative: str) -> intake_service.LedgerRow:
        rows = [
            row
            for row in intake_service.ledger(
                self.database, source_id=source_id, current_only=True
            )
            if row.relative_path == relative
        ]
        assert len(rows) == 1, rows
        return rows[0]

    def history(self, source_id: str, relative: str) -> list[intake_service.LedgerRow]:
        return [
            row for row in intake_service.ledger(self.database, source_id=source_id)
            if row.relative_path == relative
        ]

    def ledger(self, source_id: str) -> tuple[intake_service.LedgerRow, ...]:
        return intake_service.ledger(self.database, source_id=source_id)

    def ready_ids(self) -> list[int]:
        items = self.service.ready_items(scan_session_id=self.session_id)
        return [item.intake_file_id for item in items]

    def state(self, source_id: str, relative: str) -> IntakeState:
        return self.row(source_id, relative).state

    def register_all(self, source_id: str, **options: object) -> intake_service.Registration:
        items = self.service.ready_items(scan_session_id=self.session_id, source_id=source_id)
        return self.service.register(
            scan_session_id=self.session_id,
            source_id=source_id,
            intake_file_ids=[item.intake_file_id for item in items],
            identity=self.identity,
            started_by="op",
            **options,
        )


@pytest.fixture
def rig(project_session, answer_sheet_template):
    return Rig(project_session, answer_sheet_template)


# ----------------------------------------------------------------------
# The state machine, transition by transition
# ----------------------------------------------------------------------
class TestMainPath:
    def test_discovered_stabilizing_ready_registered(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "000001.jpg", jpeg(1))
        rig.poll(a)
        assert rig.state(a, "000001.jpg") is IntakeState.DISCOVERED
        rig.poll(a, advance=1)
        assert rig.state(a, "000001.jpg") is IntakeState.STABILIZING
        assert rig.fs.reads == []  # nothing read inside the quiet period
        rig.poll(a, advance=4.5)
        row = rig.row(a, "000001.jpg")
        assert row.state is IntakeState.READY
        assert row.content_sha256 == intake_service.scan_provenance.hash_bytes(jpeg(1))
        assert row.image_format == "jpeg" and row.page_count == 1
        outcome = rig.register_all(a)
        assert outcome.batch_id is not None
        row = rig.row(a, "000001.jpg")
        assert row.state is IntakeState.REGISTERED
        assert row.batch_scan_id == outcome.registered[0][1]
        assert row.registered_at is not None

    def test_quiet_period_edge(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a)
        rig.poll(a, advance=4.999)
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING
        rig.poll(a, advance=0.001)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_k_observations_are_required_even_after_a_long_wait(self, rig):
        a = rig.source(ROOT_A, policy=StabilityPolicy(min_observations=3, quiet_seconds=5))
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a)
        rig.poll(a, advance=60)
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING
        rig.poll(a, advance=1)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_repeated_reconciliation_is_idempotent(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        rig.register_all(a)
        reads = len(rig.fs.reads)
        before = intake_service.ledger(rig.database)
        for _ in range(5):
            rig.poll(a, advance=30)
        after = intake_service.ledger(rig.database)
        assert [(r.intake_file_id, r.state, r.content_sha256) for r in before] == [
            (r.intake_file_id, r.state, r.content_sha256) for r in after
        ]
        assert len(rig.fs.reads) == reads  # unchanged files are never re-hashed
        assert rig.register_all(a).batch_id is None

    def test_supported_formats(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "a.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "b.png", png(2))
        rig.fs.write(ROOT_A, "c.tif", tiff(3))
        rig.settle(a)
        assert {rig.state(a, name) for name in ("a.jpg", "b.png", "c.tif")} == {IntakeState.READY}


class TestDiversions:
    @pytest.mark.parametrize(
        "name,reason",
        [
            ("scan.tmp", IntakeReason.TEMPORARY_NAME),
            ("scan.jpg.part", IntakeReason.TEMPORARY_NAME),
            ("~scan.jpg", IntakeReason.TEMPORARY_NAME),
            (".scan.jpg", IntakeReason.TEMPORARY_NAME),
            ("scan.pdf", IntakeReason.UNSUPPORTED_SUFFIX),
            ("Thumbs.db", IntakeReason.UNSUPPORTED_SUFFIX),
            ("x_preview.jpg", IntakeReason.EXCLUDED_NAME),
        ],
    )
    def test_ignored_with_an_explainable_reason(self, rig, name, reason):
        a = rig.source(ROOT_A, exclusions=Exclusions(files=("*_preview.jpg",)))
        rig.fs.write(ROOT_A, name, jpeg())
        rig.settle(a)
        row = rig.row(a, name)
        assert row.state is IntakeState.IGNORED and row.state_reason is reason
        assert rig.fs.reads == []

    def test_vanished_before_ready_is_kept_then_reopened(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", header_only(jpeg()))
        rig.poll(a)
        rig.fs.delete(ROOT_A, "x.jpg")
        rig.poll(a, advance=1)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.VANISHED and not row.present
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a, advance=1)
        reopened = rig.row(a, "x.jpg")
        assert reopened.intake_file_id == row.intake_file_id
        assert reopened.state is IntakeState.DISCOVERED
        rig.poll(a, advance=6)
        assert rig.state(a, "x.jpg") is IntakeState.READY
        assert len(rig.history(a, "x.jpg")) == 1

    def test_permanently_truncated_becomes_unreadable_after_bounded_retries(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", header_only(jpeg(), 0.6))
        rig.settle(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.STABILIZING
        assert row.state_reason is IntakeReason.DECODE_RETRY and row.attempts == 1
        rig.poll(a, advance=1)  # inside the back-off: not read again
        assert rig.row(a, "x.jpg").attempts == 1
        rig.poll(a, advance=5)
        assert rig.row(a, "x.jpg").attempts == 2
        rig.poll(a, advance=10)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.UNREADABLE
        assert row.state_reason is IntakeReason.DECODE_FAILED
        assert "3 attempt" in row.detail
        assert rig.service.ready_items(scan_session_id=rig.session_id) == ()

    def test_zero_byte_is_never_read_and_never_unreadable(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", b"")
        for _ in range(10):
            rig.poll(a, advance=10)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.STABILIZING
        assert row.state_reason is IntakeReason.EMPTY_FILE
        assert rig.fs.reads == []

    def test_random_bytes_with_an_image_extension(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", random_bytes())
        rig.settle(a)
        rig.poll(a, advance=5.1)
        rig.poll(a, advance=10.1)
        assert rig.state(a, "x.jpg") is IntakeState.UNREADABLE

    def test_multipage_tiff_is_refused_at_file_level(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "book.tif", multipage_tiff(3))
        rig.settle(a)
        row = rig.row(a, "book.tif")
        assert row.state is IntakeState.UNSUPPORTED
        assert row.state_reason is IntakeReason.MULTIPAGE_TIFF and row.page_count == 3
        assert "not supported" in row.detail
        assert rig.service.ready_items(scan_session_id=rig.session_id) == ()

    def test_held_when_the_intended_session_is_closed(self, rig, project_session):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a)
        scan_sessions.close_scan_session(rig.database, rig.session_id, closed_by="op")
        rig.poll(a, advance=6)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.HELD
        assert row.state_reason is IntakeReason.SESSION_CLOSED
        with rig.database.session() as session:
            assert session.scalars(select(ScanBatch)).all() == []

    def test_register_refuses_a_closed_session_and_diverts_to_held(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        items = rig.service.ready_items(scan_session_id=rig.session_id)
        scan_sessions.close_scan_session(rig.database, rig.session_id, closed_by="op")
        outcome = rig.service.register(
            scan_session_id=rig.session_id, source_id=a,
            intake_file_ids=[item.intake_file_id for item in items], identity=rig.identity,
        )
        assert outcome.batch_id is None and outcome.held == (items[0].intake_file_id,)
        assert rig.state(a, "x.jpg") is IntakeState.HELD
        info = scan_sessions.get_scan_session(rig.database, rig.session_id)
        assert info is not None and info.state.value == "closed"  # never reopened
        assert len(scan_sessions.list_scan_sessions(rig.database)) == 1  # none created

    def test_register_refuses_rows_that_are_not_ready(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a)
        row = rig.row(a, "x.jpg")
        with pytest.raises(IntakeError):
            rig.service.register(
                scan_session_id=rig.session_id, source_id=a,
                intake_file_ids=[row.intake_file_id], identity=rig.identity,
            )
        assert rig.state(a, "x.jpg") is IntakeState.DISCOVERED

    def test_register_refuses_another_sources_rows(self, rig):
        a = rig.source(ROOT_A)
        b = rig.source(ROOT_B)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        item = rig.service.ready_items(scan_session_id=rig.session_id)[0]
        with pytest.raises(IntakeError):
            rig.service.register(
                scan_session_id=rig.session_id, source_id=b,
                intake_file_ids=[item.intake_file_id], identity=rig.identity,
            )


# ----------------------------------------------------------------------
# Files still being written
# ----------------------------------------------------------------------
class TestGrowingFiles:
    def test_stepped_growth_with_pauses_longer_than_the_quiet_period(self, rig):
        a = rig.source(ROOT_A)
        whole = jpeg(5)
        for step in (header_only(whole, 1 / 3), header_only(whole, 2 / 3)):
            rig.fs.write(ROOT_A, "x.jpg", step)
            # The writer pauses longer than T: the part is observed as stable,
            # read, and fails to decode - it is never ready.
            rig.settle(a)
            row = rig.row(a, "x.jpg")
            assert row.state is IntakeState.STABILIZING
            assert row.state_reason is IntakeReason.DECODE_RETRY
            rig.clock.advance(0.5)
        rig.fs.write(ROOT_A, "x.jpg", whole)
        rig.settle(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.READY
        assert row.content_sha256 == intake_service.scan_provenance.hash_bytes(whole)
        assert len(rig.history(a, "x.jpg")) == 1

    def test_kilobyte_steps_are_never_ready_early(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.png", b"")
        whole = png(9) + b""
        chunk = max(1, len(whole) // 3)
        for end in (chunk, 2 * chunk, len(whole)):
            rig.fs.write(ROOT_A, "x.png", whole[:end])
            rig.poll(a, advance=2)
            if end < len(whole):
                assert rig.state(a, "x.png") is not IntakeState.READY
        rig.poll(a, advance=6)
        assert rig.state(a, "x.png") is IntakeState.READY

    def test_same_size_changing_mtime_is_not_ready(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        for _ in range(6):
            rig.poll(a, advance=6)
            rig.fs.touch(ROOT_A, "x.jpg")
        assert rig.state(a, "x.jpg") is not IntakeState.READY
        assert rig.fs.reads == []

    def test_stable_metadata_but_locked_is_not_ready_and_not_an_attempt(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.fs.file(ROOT_A, "x.jpg").locked = True
        for _ in range(5):
            rig.poll(a, advance=6)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.STABILIZING
        assert row.state_reason is IntakeReason.LOCKED and row.attempts == 0
        rig.fs.file(ROOT_A, "x.jpg").locked = False
        rig.poll(a, advance=1)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_header_first_preallocated_file(self, rig):
        a = rig.source(ROOT_A)
        whole = jpeg(4)
        rig.fs.write(ROOT_A, "x.jpg", whole[:64] + b"\x00" * (len(whole) - 64))
        rig.settle(a)
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING
        rig.fs.write(ROOT_A, "x.jpg", whole)  # body filled in, same size, new mtime
        rig.clock.advance(1)
        rig.settle(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.READY and row.attempts == 0

    def test_file_changing_during_the_read_returns_to_stabilization(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.poll(a)
        rig.clock.advance(6)
        rig.fs.file(ROOT_A, "x.jpg").on_read = lambda: rig.fs.write(ROOT_A, "x.jpg", jpeg(2))
        rig.poll(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.STABILIZING
        assert row.state_reason is IntakeReason.CHANGED_DURING_READ
        assert row.content_sha256 is None
        rig.settle(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.READY
        assert row.content_sha256 == intake_service.scan_provenance.hash_bytes(jpeg(2))

    def test_a_file_that_becomes_valid_later(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", header_only(jpeg(), 0.5))
        rig.settle(a)
        assert rig.row(a, "x.jpg").attempts == 1
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a, advance=1)
        assert rig.row(a, "x.jpg").attempts == 0  # a new version resets the count
        rig.poll(a, advance=6)
        assert rig.state(a, "x.jpg") is IntakeState.READY


# ----------------------------------------------------------------------
# How scanners write
# ----------------------------------------------------------------------
class TestWriterPatterns:
    def test_direct_write_to_the_final_name(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", header_only(jpeg()))
        rig.poll(a, advance=1)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    @pytest.mark.parametrize("temp", ["x.tmp", "x.jpg.part"])
    def test_temporary_then_rename(self, rig, temp):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, temp, header_only(jpeg()))
        rig.poll(a)
        rig.fs.write(ROOT_A, temp, jpeg())
        rig.poll(a, advance=1)
        rig.fs.rename(ROOT_A, temp, "x.jpg")
        rig.settle(a)
        assert rig.state(a, "x.jpg") is IntakeState.READY
        temp_row = rig.history(a, temp)[0]
        assert temp_row.state is IntakeState.IGNORED and not temp_row.present

    def test_zero_byte_placeholder_then_content(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", b"")
        rig.poll(a)
        rig.poll(a, advance=10)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_held_open_after_all_bytes_written(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.fs.file(ROOT_A, "x.jpg").locked = True
        rig.settle(a)
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING
        rig.fs.file(ROOT_A, "x.jpg").locked = False
        rig.poll(a, advance=1)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_created_deleted_recreated(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.poll(a)
        rig.fs.delete(ROOT_A, "x.jpg")
        rig.poll(a, advance=1)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(2))
        rig.settle(a)
        history = rig.history(a, "x.jpg")
        assert len(history) == 1 and history[0].state is IntakeState.READY
        assert history[0].content_sha256 == intake_service.scan_provenance.hash_bytes(jpeg(2))


# ----------------------------------------------------------------------
# Collisions
# ----------------------------------------------------------------------
class TestCollisions:
    def test_same_filename_in_three_sources_is_three_files(self, rig):
        sources = [rig.source(root) for root in (ROOT_A, ROOT_B, ROOT_C)]
        for seed, root in enumerate((ROOT_A, ROOT_B, ROOT_C)):
            rig.fs.write(root, "000001.jpg", jpeg(10 + seed))
        rig.settle(*sources)
        registered = [rig.register_all(source).registered for source in sources]
        assert [len(items) for items in registered] == [1, 1, 1]
        hashes = {rig.row(source, "000001.jpg").content_sha256 for source in sources}
        assert len(hashes) == 3

    def test_path_reuse_keeps_both_histories(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "000001.jpg", jpeg(1))
        rig.settle(a)
        first = rig.register_all(a)
        first_scan = first.registered[0][1]
        rig.clock.advance(60)
        rig.fs.write(ROOT_A, "000001.jpg", jpeg(2))  # counter reset, new bytes
        rig.settle(a)
        history = rig.history(a, "000001.jpg")
        assert len(history) == 2
        old, new = history
        assert old.state is IntakeState.REGISTERED and not old.is_current
        assert old.batch_scan_id == first_scan
        assert new.is_current and new.path_reused
        assert new.previous_intake_file_id == old.intake_file_id
        assert new.state is IntakeState.READY
        second = rig.register_all(a)
        assert second.registered[0][1] != first_scan
        with rig.database.session() as session:
            scan = session.get(BatchScan, first_scan)
            assert scan is not None
            assert scan.content_sha256 == intake_service.scan_provenance.hash_bytes(jpeg(1))

    def test_touched_but_unchanged_content_is_not_new(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.settle(a)
        rig.register_all(a)
        rig.fs.touch(ROOT_A, "x.jpg")
        rig.settle(a)
        history = rig.history(a, "x.jpg")
        assert [row.state for row in history] == [IntakeState.REGISTERED, IntakeState.IGNORED]
        assert history[1].state_reason is IntakeReason.UNCHANGED_CONTENT
        assert history[0].is_current and not history[1].is_current
        assert rig.register_all(a).batch_id is None

    def test_same_bytes_different_path_same_source_is_duplicate_content(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "000001.jpg", jpeg(1))
        rig.settle(a)
        first = rig.register_all(a)
        rig.fs.write(ROOT_A, "copy/000123.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "000123.jpg", jpeg(1))
        rig.settle(a)
        second = rig.register_all(a)
        assert [item[1] for item in second.duplicates] == [first.registered[0][1]]
        row = rig.row(a, "000123.jpg")
        assert row.state is IntakeState.DUPLICATE_CONTENT
        assert row.duplicate_of_scan_id == first.registered[0][1]
        with rig.database.session() as session:
            scan = session.get(BatchScan, row.batch_scan_id)
            assert scan is not None and scan.status == "duplicate"

    def test_same_bytes_twice_in_one_registration(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "2.jpg", jpeg(1))
        rig.settle(a)
        outcome = rig.register_all(a)
        assert len(outcome.registered) == 1 and len(outcome.duplicates) == 1
        assert outcome.duplicates[0][1] == outcome.registered[0][1]
        assert rig.state(a, "2.jpg") is IntakeState.DUPLICATE_CONTENT

    def test_same_bytes_across_sources(self, rig):
        a = rig.source(ROOT_A)
        b = rig.source(ROOT_B)
        rig.fs.write(ROOT_A, "000123.jpg", jpeg(3))
        rig.fs.write(ROOT_B, "004599.jpg", jpeg(3))
        rig.settle(a, b)
        first = rig.register_all(a)
        second = rig.register_all(b)
        copy_id = rig.row(b, "004599.jpg").intake_file_id
        assert second.duplicates == ((copy_id, first.registered[0][1]),)
        assert rig.row(a, "000123.jpg").state is IntakeState.REGISTERED
        assert rig.row(b, "004599.jpg").state is IntakeState.DUPLICATE_CONTENT

    def test_same_bytes_in_another_session_are_not_merged(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(3))
        rig.settle(a)
        first = rig.register_all(a)
        other = scan_sessions.create_scan_session(rig.database, name="Resit", created_by="op")
        b = rig.source(ROOT_B, attach=False)
        intake_service.attach_source(rig.database, b, other.scan_session_id, actor="op")
        rig.fs.write(ROOT_B, "y.jpg", jpeg(3))
        rig.settle(b)
        items = rig.service.ready_items(scan_session_id=other.scan_session_id)
        second = rig.service.register(
            scan_session_id=other.scan_session_id, source_id=b,
            intake_file_ids=[item.intake_file_id for item in items], identity=rig.identity,
        )
        assert first.registered and second.registered and not second.duplicates
        assert rig.row(b, "y.jpg").state is IntakeState.REGISTERED

    def test_copy_of_a_rejected_scan_is_linked_as_a_reimport(self, rig):
        from sqlalchemy import update

        from omr_scanner.domain.scan_lifecycle import RejectionReason
        from omr_scanner.services import scan_lifecycle

        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(4))
        rig.settle(a)
        scan_id = rig.register_all(a).registered[0][1]
        with rig.database.session() as session:
            session.execute(
                update(BatchScan).where(BatchScan.scan_id == scan_id).values(status="completed")
            )
        scan_lifecycle.reject_scan(
            rig.database, scan_id, reviewer="op", reason=RejectionReason.FOLDED
        )
        rig.fs.write(ROOT_A, "again.jpg", jpeg(4))
        rig.settle(a)
        outcome = rig.register_all(a)
        assert outcome.duplicates and outcome.duplicates[0][1] == scan_id
        row = rig.row(a, "again.jpg")
        assert row.state is IntakeState.DUPLICATE_CONTENT
        assert "reimport_of_rejected" in row.detail


# ----------------------------------------------------------------------
# Recursion and exclusions
# ----------------------------------------------------------------------
class TestRecursion:
    def test_recursive_disabled_sees_only_the_root(self, rig):
        a = rig.source(ROOT_A, recursive=False)
        rig.fs.write(ROOT_A, "a.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "sub/a.jpg", jpeg(2))
        rig.settle(a)
        assert [row.relative_path for row in intake_service.ledger(rig.database)] == ["a.jpg"]

    def test_recursive_same_name_in_two_folders(self, rig):
        a = rig.source(ROOT_A, recursive=True)
        rig.fs.write(ROOT_A, "day1/000001.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "day2/000001.jpg", jpeg(2))
        rig.fs.write(ROOT_A, "day2/deep/er/000001.jpg", jpeg(3))
        rig.settle(a)
        rows = intake_service.ledger(rig.database, source_id=a)
        assert sorted(row.relative_path for row in rows) == [
            "day1/000001.jpg", "day2/000001.jpg", "day2/deep/er/000001.jpg",
        ]
        assert {row.state for row in rows} == {IntakeState.READY}

    def test_folder_exclusions_and_dot_folders(self, rig):
        a = rig.source(ROOT_A, recursive=True, exclusions=Exclusions(folders=("archive",)))
        rig.fs.write(ROOT_A, "archive/a.jpg", jpeg(1))
        rig.fs.write(ROOT_A, "x/archive/b.jpg", jpeg(2))
        rig.fs.write(ROOT_A, ".sync/c.jpg", jpeg(3))
        rig.fs.write(ROOT_A, "keep/d.jpg", jpeg(4))
        rig.settle(a)
        assert [row.relative_path for row in intake_service.ledger(rig.database)] == ["keep/d.jpg"]


# ----------------------------------------------------------------------
# Reachability
# ----------------------------------------------------------------------
class TestReachability:
    def test_one_source_unreachable_does_not_touch_the_others(self, rig):
        a, b, c = (rig.source(root) for root in (ROOT_A, ROOT_B, ROOT_C))
        for seed, root in enumerate((ROOT_A, ROOT_B, ROOT_C)):
            rig.fs.write(root, "1.jpg", jpeg(seed))
        rig.settle(a, b, c)
        rig.register_all(b)
        rig.fs.write(ROOT_B, "2.jpg", jpeg(20))
        rig.poll(b)
        rig.fs.unreachable.add(ROOT_B)
        before = {row.intake_file_id: row for row in rig.ledger(b)}
        rig.fs.write(ROOT_A, "2.jpg", jpeg(21))
        reports = rig.service.reconcile_all()
        rig.clock.advance(6)
        rig.service.reconcile_all()
        assert [r.reachability for r in reports] == [
            Reachability.ONLINE, Reachability.UNREACHABLE, Reachability.ONLINE
        ]
        after = {row.intake_file_id: row for row in rig.ledger(b)}
        assert {k: (v.state, v.present) for k, v in after.items()} == {
            k: (v.state, v.present) for k, v in before.items()
        }
        assert IntakeState.VANISHED not in {row.state for row in after.values()}
        assert rig.state(a, "2.jpg") is IntakeState.READY
        source = intake_service.get_source(rig.database, b)
        assert source is not None and source.reachability is Reachability.UNREACHABLE
        assert "not reachable" in source.reachability_detail
        # Back online: a full reconciliation finds what arrived meanwhile.
        rig.fs.unreachable.discard(ROOT_B)
        rig.fs.write(ROOT_B, "3.jpg", jpeg(22))
        rig.settle(b)
        assert {rig.state(b, n) for n in ("1.jpg", "2.jpg", "3.jpg")} == {
            IntakeState.REGISTERED, IntakeState.READY
        }
        assert rig.state(b, "3.jpg") is IntakeState.READY
        assert len(intake_service.ledger(rig.database, source_id=b)) == 3  # nothing duplicated

    def test_permission_denied_then_restored(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
        rig.settle(a)
        rig.fs.denied.add(ROOT_A)
        report = rig.poll(a, advance=1)[0]
        assert report.reachability is Reachability.PERMISSION_DENIED
        assert rig.state(a, "1.jpg") is IntakeState.READY
        rig.fs.denied.discard(ROOT_A)
        assert rig.poll(a, advance=1)[0].reachability is Reachability.ONLINE
        assert len(intake_service.ledger(rig.database)) == 1

    def test_an_unlistable_sub_folder_is_not_evidence_of_vanishing(self, rig):
        a = rig.source(ROOT_A, recursive=True)
        rig.fs.write(ROOT_A, "sub/1.jpg", jpeg(1))
        rig.settle(a)
        rig.fs.unlistable_folders[ROOT_A] = {"sub"}
        report = rig.poll(a, advance=1)[0]
        assert report.reachability is Reachability.ONLINE and report.unlisted_folders == ("sub",)
        assert rig.state(a, "sub/1.jpg") is IntakeState.READY

    def test_disabled_source_is_not_listed(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
        intake_service.set_source_enabled(rig.database, a, False, actor="op")
        report = rig.poll(a)[0]
        assert report.reachability is Reachability.DISABLED
        assert intake_service.ledger(rig.database) == ()


# ----------------------------------------------------------------------
# Restart
# ----------------------------------------------------------------------
class TestRestart:
    def test_stabilization_is_not_trusted_across_a_restart(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.poll(a)
        rig.poll(a, advance=4)
        rig.restart()
        row = rig.row(a, "x.jpg")
        assert row.observations == 0 and row.stable_since is None
        rig.poll(a, advance=10)  # long after: still needs fresh observations over T
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING
        rig.poll(a, advance=5.1)
        assert rig.state(a, "x.jpg") is IntakeState.READY

    def test_ready_is_reverified_and_keeps_its_order(self, rig):
        a = rig.source(ROOT_A)
        for index in range(3):
            rig.fs.write(ROOT_A, f"{index}.jpg", jpeg(index))
            rig.settle(a)
        order = rig.ready_ids()
        reads = len(rig.fs.reads)
        rig.restart()
        assert rig.service.ready_items(scan_session_id=rig.session_id) == ()
        assert {rig.row(a, f"{i}.jpg").reverify_required for i in range(3)} == {True}
        rig.poll(a, advance=1)
        assert len(rig.fs.reads) == reads + 3  # each read once more
        again = rig.ready_ids()
        assert again == order
        outcome = rig.register_all(a)
        assert len(outcome.registered) == 3

    def test_ready_file_replaced_while_stopped_is_not_registered_as_the_old_bytes(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.settle(a)
        rig.restart()
        rig.fs.write(ROOT_A, "x.jpg", jpeg(2))
        rig.settle(a)
        row = rig.row(a, "x.jpg")
        assert row.state is IntakeState.READY
        assert row.content_sha256 == intake_service.scan_provenance.hash_bytes(jpeg(2))

    def test_files_arriving_while_stopped_are_found(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
        rig.settle(a)
        rig.register_all(a)
        for index in range(2, 6):
            rig.fs.write(ROOT_A, f"{index}.jpg", jpeg(index))
        rig.restart()
        rig.settle(a)
        outcome = rig.register_all(a)
        assert len(outcome.registered) == 4
        assert len(intake_service.ledger(rig.database)) == 5

    def test_registration_crash_before_duplicate_link_is_completed(self, rig, monkeypatch):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
        rig.settle(a)
        rig.register_all(a)
        rig.fs.write(ROOT_A, "2.jpg", jpeg(1))
        rig.settle(a)
        monkeypatch.setattr(intake_service, "link_exact_duplicates", lambda *_args, **_kwargs: [])
        rig.register_all(a)
        assert rig.state(a, "2.jpg") is IntakeState.REGISTERED  # the "crash" left it here
        monkeypatch.undo()
        rig.restart()
        assert rig.state(a, "2.jpg") is IntakeState.DUPLICATE_CONTENT
        assert rig.service.last_recovery.duplicates_completed == 1


# ----------------------------------------------------------------------
# Registration API
# ----------------------------------------------------------------------
class TestRegistration:
    def test_order_is_ready_time_then_id(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "late.jpg", jpeg(1))
        rig.poll(a)
        rig.fs.write(ROOT_A, "early.jpg", jpeg(2))
        rig.fs.write(ROOT_A, "z.jpg", jpeg(3))
        rig.poll(a, advance=1)
        rig.poll(a, advance=4.5)  # late.jpg ready now; early/z not yet
        rig.poll(a, advance=1)
        items = rig.service.ready_items(scan_session_id=rig.session_id)
        names = [item.relative_path for item in items]
        assert names == ["late.jpg", "early.jpg", "z.jpg"]
        outcome = rig.register_all(a)
        with rig.database.session() as session:
            members = session.execute(
                select(BatchScan.intake_file_id).where(BatchScan.batch_id == outcome.batch_id)
                .order_by(BatchScan.batch_index)
            ).scalars().all()
        assert list(members) == [item.intake_file_id for item in items]

    def test_repeated_registration_is_idempotent(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        rig.settle(a)
        ids = rig.ready_ids()
        first = rig.service.register(
            scan_session_id=rig.session_id, source_id=a, intake_file_ids=ids, identity=rig.identity
        )
        again = rig.service.register(
            scan_session_id=rig.session_id, source_id=a, intake_file_ids=ids, identity=rig.identity
        )
        assert again.batch_id is None and again.already_registered == tuple(ids)
        with rig.database.session() as session:
            assert len(session.scalars(select(BatchScan)).all()) == 1
            batch = session.get(ScanBatch, first.batch_id)
            assert batch is not None and batch.sealed_at is not None and batch.source_id == a

    def test_registration_copies_into_the_project_and_records_provenance(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "000001.jpg", jpeg(7))
        rig.settle(a)
        outcome = rig.register_all(a)
        row = rig.row(a, "000001.jpg")
        digest = intake_service.scan_provenance.hash_bytes(jpeg(7))
        assert row.ingest_path == f"scans_original/intake/{digest[:2]}/{digest}.jpg"
        copy = rig.project.root / row.ingest_path
        assert copy.read_bytes() == jpeg(7)
        with rig.database.session() as session:
            scan = session.get(BatchScan, outcome.registered[0][1])
            assert scan is not None
            assert scan.source_path == str(copy)
            assert scan.intake_file_id == row.intake_file_id
            assert scan.status == "pending"  # registered, not recognised
        assert row.absolute_path == rig.fs.path(ROOT_A, "000001.jpg")  # original provenance

    def test_source_changed_after_ready_goes_back_to_stabilizing(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.settle(a)
        ids = rig.ready_ids()
        rig.fs.write(ROOT_A, "x.jpg", jpeg(2), mtime_ns=rig.fs.file(ROOT_A, "x.jpg").mtime_ns)
        outcome = rig.service.register(
            scan_session_id=rig.session_id, source_id=a, intake_file_ids=ids, identity=rig.identity
        )
        assert outcome.batch_id is None and outcome.returned == tuple(ids)
        assert rig.state(a, "x.jpg") is IntakeState.STABILIZING

    def test_disk_full_leaves_the_row_ready(self, rig):
        class FullDisk:
            def __init__(self, path: object) -> None:
                raise OSError(28, "No space left on device")

        store = IngestStore(rig.project.root, opener=FullDisk)
        rig.restart(store=store)
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg(1))
        rig.settle(a)
        outcome = rig.register_all(a)
        assert outcome.batch_id is None and "no space" in outcome.failed[0][1]
        assert rig.state(a, "x.jpg") is IntakeState.READY
        assert not list((rig.project.root / "scans_original" / "intake").rglob("*.part"))

    def test_ledger_holds_no_recognition_state(self):
        names = {column.name for column in IntakeFile.__table__.columns}
        for forbidden in ("status", "outcome", "identifier_value", "result_json", "attempt_count"):
            assert forbidden not in names

    def test_source_configuration_is_audited_polls_are_not(self, rig):
        a = rig.source(ROOT_A)
        rig.fs.write(ROOT_A, "x.jpg", jpeg())
        for _ in range(20):
            rig.poll(a, advance=3)
        with rig.database.session() as session:
            actions = session.scalars(
                select(AuditEvent.action).where(AuditEvent.entity_type == "intake_source")
            ).all()
        assert sorted(actions) == ["source_attached", "source_created"]
