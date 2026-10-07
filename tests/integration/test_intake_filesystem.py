"""Intake on real temporary directories (0.1.1 revised phase 5).

The fake-filesystem suite (``test_intake_ledger.py``) proves the state machine
edge by edge; this one proves the real :class:`OsFileSystem` and
:class:`IngestStore` against real files: listing (spaces, Unicode, nesting,
long paths), a real Windows sharing violation, three sources with
counter-named files, copy integrity and crash points of the copy, and that
source folders are never modified. The clock is still injected - stability is
judged on real metadata over fake seconds, so nothing sleeps.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.intake_fakes import (
    CorruptingWriter,
    FakeClock,
    InterruptedWriter,
    header_only,
    jpeg,
    png,
)

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.intake import Exclusions, IntakeReason, IntakeState, StabilityPolicy
from omr_scanner.services import batch_store, scan_provenance, scan_sessions
from omr_scanner.services import intake as intake_service
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.intake_fs import (
    TEMP_MARKER,
    IngestError,
    IngestStore,
    OsFileSystem,
    extended_path,
)

POLICY = StabilityPolicy(min_observations=2, quiet_seconds=5)


def snapshot_tree(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under ``root``: size, mtime_ns, SHA-256."""
    found: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            info = path.stat()
            found[path.relative_to(root).as_posix()] = (
                info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
            )
    return found


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class Real:
    def __init__(self, project_session, template, tmp_path: Path) -> None:
        self.project = project_session
        self.database = project_session.database
        self.clock = FakeClock()
        self.identity = batch_store.BatchIdentity.of(template)
        self.session_id = scan_sessions.create_scan_session(
            self.database, name="Exam", created_by="op"
        ).scan_session_id
        self.scanners = tmp_path / "scanners"
        self.store: IngestStore | None = None
        self.service = self.restart()

    def restart(self, store: IngestStore | None = None) -> IntakeService:
        self.store = store
        self.service = IntakeService(
            self.database, self.project.root, fs=OsFileSystem(), clock=self.clock, store=store
        )
        return self.service

    def source(self, name: str, **options: object) -> tuple[str, Path]:
        folder = self.scanners / name
        folder.mkdir(parents=True, exist_ok=True)
        options.setdefault("policy", POLICY)
        info = intake_service.create_source(
            self.database, label=name, root_path=str(folder), clock=self.clock, **options
        )
        intake_service.attach_source(self.database, info.source_id, self.session_id)
        return info.source_id, folder

    def settle(self, *sources: str) -> None:
        for source in sources:
            self.service.reconcile(source)
        self.clock.advance(POLICY.quiet_seconds + 0.1)
        for source in sources:
            self.service.reconcile(source)

    def register(self, source: str) -> intake_service.Registration:
        items = self.service.ready_items(scan_session_id=self.session_id, source_id=source)
        return self.service.register(
            scan_session_id=self.session_id, source_id=source,
            intake_file_ids=[item.intake_file_id for item in items], identity=self.identity,
        )

    def states(self, source: str) -> dict[str, IntakeState]:
        return {
            row.relative_path: row.state
            for row in intake_service.ledger(self.database, source_id=source, current_only=True)
        }


@pytest.fixture
def real(project_session, answer_sheet_template, tmp_path):
    return Real(project_session, answer_sheet_template, tmp_path)


class TestListing:
    def test_spaces_unicode_and_nesting(self, tmp_path):
        root = tmp_path / "Scanner A (Room 2)"
        write(root / "000001.jpg", b"x")
        write(root / "পরীক্ষা ২০২৬" / "শিট 1.jpg", b"y")
        write(root / "a" / "b" / "c" / "deep file.png", b"z")
        write(root / ".sync" / "hidden.jpg", b"h")
        flat = OsFileSystem().list_source(str(root), recursive=False, exclusions=Exclusions())
        assert [item.relative_path for item in flat.files] == ["000001.jpg"]
        deep = OsFileSystem().list_source(str(root), recursive=True, exclusions=Exclusions())
        assert sorted(item.relative_path for item in deep.files) == [
            "000001.jpg", "a/b/c/deep file.png", "পরীক্ষা ২০২৬/শিট 1.jpg",
        ]
        assert deep.skipped_folders == (".sync",)
        unicode_item = next(item for item in deep.files if "শিট" in item.relative_path)
        assert Path(unicode_item.absolute_path).read_bytes() == b"y"

    def test_missing_root_is_unreachable_not_empty(self, tmp_path):
        from omr_scanner.domain.intake import Reachability
        from omr_scanner.services.intake_fs import SourceListingError

        with pytest.raises(SourceListingError) as raised:
            OsFileSystem().list_source(
                str(tmp_path / "gone"), recursive=True, exclusions=Exclusions()
            )
        assert raised.value.reachability is Reachability.UNREACHABLE

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows folder junctions")
    def test_a_folder_link_removed_mid_pass_vanishes_nothing(
        self, project_session, answer_sheet_template, tmp_path
    ):
        """A folder junction removed after the listing and before the reads.

        That is how the phase 9 campaign takes a scanner offline: the reads fail
        as *file not found*, yet nothing is marked vanished and the source is
        unreachable; reconnected, the same rows become ready.
        """
        import _winapi

        from omr_scanner.domain.intake import Reachability
        from omr_scanner.services.intake_fs import ReadSnapshot

        target = tmp_path / "disk"
        link = tmp_path / "share"
        for seed in range(3):
            write(target / f"00000{seed}.png", png(seed))
        _winapi.CreateJunction(str(target), str(link))

        class DropsOnFirstRead(OsFileSystem):
            armed = False

            def read_snapshot(self, path: str) -> ReadSnapshot:
                if self.armed:
                    self.armed = False
                    link.rmdir()  # removes the junction, not the files
                return super().read_snapshot(path)

        real = Real(project_session, answer_sheet_template, tmp_path)
        fs = DropsOnFirstRead()
        real.service = IntakeService(real.database, real.project.root, fs=fs, clock=real.clock)
        info = intake_service.create_source(real.database, label="Linked", root_path=str(link),
                                            clock=real.clock, policy=POLICY)
        intake_service.attach_source(real.database, info.source_id, real.session_id)
        real.service.reconcile(info.source_id)
        real.clock.advance(POLICY.quiet_seconds + 0.1)
        fs.armed = True
        report = real.service.reconcile(info.source_id)
        assert not fs.armed, "the pass read nothing"
        assert report.reachability is Reachability.UNREACHABLE
        assert IntakeState.VANISHED not in set(real.states(info.source_id).values())
        _winapi.CreateJunction(str(target), str(link))
        real.settle(info.source_id)
        assert set(real.states(info.source_id).values()) == {IntakeState.READY}
        assert len(intake_service.ledger(real.database, source_id=info.source_id)) == 3
        link.rmdir()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
    def test_extended_path_forms(self):
        long_local = "C:\\" + "\\".join(["d" * 50] * 6) + "\\x.jpg"
        assert extended_path(long_local).startswith("\\\\?\\C:\\")
        long_unc = "\\\\scanner-a\\scans\\" + "\\".join(["d" * 50] * 6)
        assert extended_path(long_unc).startswith("\\\\?\\UNC\\scanner-a\\scans\\")
        assert extended_path("C:\\short.jpg") == "C:\\short.jpg"
        assert extended_path("\\\\?\\C:\\x") == "\\\\?\\C:\\x"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows long paths")
    def test_long_paths_are_listed_and_read(self, tmp_path):
        root = tmp_path / "long"
        folder = Path(extended_path(str(root / ("n" * 60) / ("o" * 60) / ("p" * 60) / ("q" * 60))))
        try:
            folder.mkdir(parents=True)
            (folder / "000001.jpg").write_bytes(jpeg(1))
        except OSError:
            pytest.skip("this filesystem refuses long paths")
        listing = OsFileSystem().list_source(str(root), recursive=True, exclusions=Exclusions())
        (item,) = listing.files
        assert len(item.absolute_path) > 260
        snapshot = OsFileSystem().read_snapshot(item.absolute_path)
        assert snapshot.data == jpeg(1)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing violation")
    def test_a_real_sharing_violation_is_locked_not_an_error(self, tmp_path):
        import ctypes
        from ctypes import wintypes

        path = write(tmp_path / "held.jpg", jpeg())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path), 0x40000000, 0, None, 3, 0x80, None  # GENERIC_WRITE, no sharing
        )
        assert handle not in (None, wintypes.HANDLE(-1).value)
        try:
            snapshot = OsFileSystem().read_snapshot(str(path))
        finally:
            kernel32.CloseHandle(handle)
        assert snapshot.data is None and snapshot.failure is IntakeReason.LOCKED
        assert OsFileSystem().read_snapshot(str(path)).data == jpeg()


class TestThreeSources:
    def test_counter_names_across_three_sources(self, real):
        sources = {}
        expected: dict[tuple[str, str], str] = {}
        for index, name in enumerate(("Scanner A", "Scanner B", "Scanner C")):
            source_id, folder = real.source(name)
            sources[name] = (source_id, folder)
            for number in range(1, 4):
                data = jpeg(100 * index + number)
                write(folder / f"{number:06d}.jpg", data)
                expected[(source_id, f"{number:06d}.jpg")] = hashlib.sha256(data).hexdigest()
        # Identical bytes under different names on A and C.
        same = jpeg(999)
        write(sources["Scanner A"][1] / "000004.jpg", same)
        write(sources["Scanner C"][1] / "000777.jpg", same)
        before = {name: snapshot_tree(folder) for name, (_id, folder) in sources.items()}
        ids = [source_id for source_id, _folder in sources.values()]
        real.settle(*ids)
        outcomes = {name: real.register(source_id) for name, (source_id, _f) in sources.items()}
        assert len(outcomes["Scanner A"].registered) == 4
        assert len(outcomes["Scanner B"].registered) == 3
        assert len(outcomes["Scanner C"].registered) == 4
        assert len(outcomes["Scanner C"].duplicates) == 1  # the copy of A's 000004
        a_scan = dict(outcomes["Scanner A"].registered)[
            next(r.intake_file_id for r in intake_service.ledger(real.database)
                 if r.relative_path == "000004.jpg")
        ]
        assert outcomes["Scanner C"].duplicates[0][1] == a_scan
        for (source_id, relative), digest in expected.items():
            rows = [r for r in intake_service.ledger(real.database, source_id=source_id)
                    if r.relative_path == relative]
            assert len(rows) == 1 and rows[0].content_sha256 == digest
            assert rows[0].state is IntakeState.REGISTERED
        # Sources untouched: same names, sizes, times and bytes.
        assert {name: snapshot_tree(folder) for name, (_i, folder) in sources.items()} == before
        # Only unique content was copied into the project.
        copies = list((real.project.root / "scans_original" / "intake").rglob("*.jpg"))
        assert len(copies) == 10


class TestCopyIntegrity:
    def test_project_copy_survives_source_deletion(self, real):
        source, folder = real.source("A")
        write(folder / "1.jpg", jpeg(1))
        real.settle(source)
        outcome = real.register(source)
        (folder / "1.jpg").unlink()
        with real.database.session() as session:
            scan = session.get(BatchScan, outcome.registered[0][1])
            assert scan is not None
            copy = Path(scan.source_path)
        assert copy.read_bytes() == jpeg(1)
        assert scan_provenance.hash_file(copy) == scan.content_sha256
        real.service.reconcile(source)
        assert real.states(source)["1.jpg"] is IntakeState.REGISTERED  # history, not vanished

    def test_interrupted_copy_does_not_register_and_leaves_no_half_file(self, real):
        real.restart(IngestStore(real.project.root, opener=InterruptedWriter))
        source, folder = real.source("A")
        write(folder / "1.jpg", jpeg(1))
        real.settle(source)
        outcome = real.register(source)
        assert outcome.batch_id is None and outcome.failed
        assert real.states(source)["1.jpg"] is IntakeState.READY
        store_root = real.project.root / "scans_original" / "intake"
        assert not [p for p in store_root.rglob("*") if p.is_file()]
        real.restart()
        assert real.register(source).batch_id is None  # ready again only once re-verified
        real.service.reconcile(source)
        assert len(real.register(source).registered) == 1

    def test_a_corrupting_writer_is_caught_by_the_copy_hash(self, real):
        real.restart(IngestStore(real.project.root, opener=CorruptingWriter))
        source, folder = real.source("A")
        write(folder / "1.jpg", jpeg(1))
        real.settle(source)
        outcome = real.register(source)
        assert outcome.batch_id is None and "did not verify" in outcome.failed[0][1]
        assert not list((real.project.root / "scans_original" / "intake").rglob("*.jpg"))

    def test_restart_after_a_temporary_copy_cleans_it(self, real):
        store = IngestStore(real.project.root)
        digest = hashlib.sha256(jpeg(1)).hexdigest()
        final = store.destination(digest, ".jpg")
        leftover = final.with_name(f"{final.name}{TEMP_MARKER}dead.part")
        write(leftover, jpeg(1)[:100])
        real.restart(store)
        assert not leftover.exists()
        assert real.service.last_recovery.temporaries_removed == 1

    def test_crash_after_rename_before_commit_reuses_the_copy(self, real):
        source, folder = real.source("A")
        write(folder / "1.jpg", jpeg(1))
        real.settle(source)
        store = IngestStore(real.project.root)
        digest = hashlib.sha256(jpeg(1)).hexdigest()
        placed = store.put(jpeg(1), digest, ".jpg")  # the copy landed, the ledger did not
        assert not placed.reused
        real.restart(store)
        real.settle(source)
        outcome = real.register(source)
        assert len(outcome.registered) == 1
        assert len(list(store.root.rglob("*.jpg"))) == 1

    def test_destination_with_different_content_is_set_aside_not_trusted(self, real):
        store = IngestStore(real.project.root)
        digest = hashlib.sha256(jpeg(1)).hexdigest()
        final = write(store.destination(digest, ".jpg"), b"tampered")
        result = store.put(jpeg(1), digest, ".jpg")
        assert result.path == final and final.read_bytes() == jpeg(1)
        assert final.with_name(final.name + ".mismatch-1").read_bytes() == b"tampered"

    def test_wrong_bytes_are_refused_before_writing(self, real):
        store = IngestStore(real.project.root)
        with pytest.raises(IngestError) as raised:
            store.put(jpeg(1), "0" * 64, ".jpg")
        assert raised.value.reason is IntakeReason.SOURCE_CHANGED
        assert not store.root.exists() or not list(store.root.rglob("*"))

    def test_reference_mode_reads_in_place(self, real):
        from omr_scanner.domain.intake import IngestMode

        source, folder = real.source("A", ingest_mode=IngestMode.REFERENCE)
        write(folder / "1.jpg", jpeg(1))
        real.settle(source)
        outcome = real.register(source)
        with real.database.session() as session:
            scan = session.get(BatchScan, outcome.registered[0][1])
            assert scan is not None and Path(scan.source_path) == folder / "1.jpg"
        assert not (real.project.root / "scans_original" / "intake").exists()


class TestRealReopen:
    """The project really closed and reopened (``open_project``), real files."""

    def make(self, tmp_path, template):
        from omr_scanner.services import create_project

        project = create_project(tmp_path, "reopen")
        folder = tmp_path / "scanner"
        folder.mkdir()
        session_id = scan_sessions.create_scan_session(project.database, name="E").scan_session_id
        source = intake_service.create_source(
            project.database, label="A", root_path=str(folder), policy=POLICY
        )
        intake_service.attach_source(project.database, source.source_id, session_id)
        return project, folder, session_id, source.source_id

    def service(self, project, clock):
        return IntakeService(project.database, project.root, fs=OsFileSystem(), clock=clock)

    def test_restart_mid_stabilisation(self, tmp_path, answer_sheet_template):
        from omr_scanner.services import open_project

        project, folder, session_id, source = self.make(tmp_path, answer_sheet_template)
        clock = FakeClock()
        service = self.service(project, clock)
        write(folder / "1.jpg", jpeg(1))
        service.reconcile(source)
        clock.advance(4)
        service.reconcile(source)  # two observations, quiet period not yet over
        root = project.root
        project.close()
        with open_project(root) as reopened:
            clock.advance(60)
            service = self.service(reopened, clock)
            (row,) = intake_service.ledger(reopened.database)
            assert row.observations == 0 and row.stable_since is None
            service.reconcile(source)
            assert intake_service.ledger(reopened.database)[0].state is IntakeState.STABILIZING
            clock.advance(5.1)
            service.reconcile(source)
            items = service.ready_items(scan_session_id=session_id)
            outcome = service.register(
                scan_session_id=session_id, source_id=source,
                intake_file_ids=[item.intake_file_id for item in items],
                identity=batch_store.BatchIdentity.of(answer_sheet_template),
            )
            assert len(outcome.registered) == 1
            with reopened.database.session() as session:
                assert len(session.scalars(select(BatchScan)).all()) == 1

    def test_ready_but_unregistered_survives_a_reopen_exactly_once(
        self, tmp_path, answer_sheet_template
    ):
        from omr_scanner.services import open_project

        project, folder, session_id, source = self.make(tmp_path, answer_sheet_template)
        clock = FakeClock()
        service = self.service(project, clock)
        write(folder / "1.jpg", jpeg(1))
        write(folder / "2.jpg", jpeg(2))
        service.reconcile(source)
        clock.advance(6)
        service.reconcile(source)
        order = [item.intake_file_id for item in service.ready_items(scan_session_id=session_id)]
        assert len(order) == 2
        root = project.root
        project.close()
        for _ in range(2):  # reopen twice: still exactly once
            with open_project(root) as reopened:
                service = self.service(reopened, clock)
                assert service.ready_items(scan_session_id=session_id) == ()
                service.reconcile(source)  # re-verified
                items = service.ready_items(scan_session_id=session_id)
                if items:
                    assert [item.intake_file_id for item in items] == order
                    outcome = service.register(
                        scan_session_id=session_id, source_id=source,
                        intake_file_ids=[item.intake_file_id for item in items],
                        identity=batch_store.BatchIdentity.of(answer_sheet_template),
                    )
                    assert len(outcome.registered) == 2
        with open_project(root, read_only=True) as final:
            with final.database.session() as session:
                assert len(session.scalars(select(BatchScan)).all()) == 2
            rows = intake_service.ledger(final.database)
            assert [row.state for row in rows] == [IntakeState.REGISTERED] * 2


class TestRealWriters:
    def test_partial_jpeg_and_partial_png_are_never_ready(self, real):
        source, folder = real.source("A")
        write(folder / "a.jpg", header_only(jpeg(1), 0.7))
        write(folder / "b.png", header_only(png(2), 0.7))
        real.settle(source)
        assert set(real.states(source).values()) == {IntakeState.STABILIZING}
        write(folder / "a.jpg", jpeg(1))
        write(folder / "b.png", png(2))
        os.utime(folder / "a.jpg", ns=(1, 2_000_000_000_000_000_000))
        os.utime(folder / "b.png", ns=(1, 2_000_000_000_000_000_000))
        real.settle(source)
        assert set(real.states(source).values()) == {IntakeState.READY}

    def test_path_reuse_on_disk(self, real):
        source, folder = real.source("A")
        write(folder / "000001.jpg", jpeg(1))
        real.settle(source)
        first = real.register(source)
        write(folder / "000001.jpg", jpeg(2))
        os.utime(folder / "000001.jpg", ns=(1, 2_000_000_000_000_000_000))
        real.settle(source)
        second = real.register(source)
        rows = intake_service.ledger(real.database, source_id=source)
        assert [(r.state, r.path_reused) for r in rows] == [
            (IntakeState.REGISTERED, False), (IntakeState.REGISTERED, True)
        ]
        with real.database.session() as session:
            hashes = dict(
                session.execute(select(BatchScan.scan_id, BatchScan.content_sha256)).all()
            )
        assert hashes[first.registered[0][1]] == hashlib.sha256(jpeg(1)).hexdigest()
        assert hashes[second.registered[0][1]] == hashlib.sha256(jpeg(2)).hexdigest()
