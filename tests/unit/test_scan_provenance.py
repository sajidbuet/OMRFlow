"""Unit tests for content-hash provenance (Phase 10, §9/§10)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omr_scanner.database.engine import open_project_database
from omr_scanner.services import batch_store, scan_provenance


@pytest.fixture
def database(tmp_path: Path):
    db_path = tmp_path / "database.sqlite"
    handle = open_project_database(db_path, create=True)
    yield handle
    handle.close()


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


class TestHashing:
    def test_hash_file_matches_hashlib(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "a.png", b"hello world" * 1000)
        expected = hashlib.sha256(b"hello world" * 1000).hexdigest()
        assert scan_provenance.hash_file(path) == expected

    def test_hash_file_is_bounded_memory_for_a_large_file(self, tmp_path: Path) -> None:
        # Not a real memory-profiling test (that would be flaky); asserts the
        # chunked read loop actually terminates and matches hashlib for a file
        # spanning multiple read chunks.
        path = tmp_path / "big.bin"
        chunk = b"x" * scan_provenance._READ_CHUNK_BYTES
        with path.open("wb") as handle:
            handle.write(chunk)
            handle.write(b"tail")
        expected = hashlib.sha256(chunk + b"tail").hexdigest()
        assert scan_provenance.hash_file(path) == expected

    def test_hash_bytes_matches_hashlib(self) -> None:
        assert scan_provenance.hash_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_is_virtual_source(self) -> None:
        assert scan_provenance.is_virtual_source("stress:12345-000042")
        assert not scan_provenance.is_virtual_source("C:/scans/a.png")


class TestComputeHashesForBatch:
    def test_real_files_are_hashed_and_persisted(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"AAA")
        b = _write(tmp_path / "b.png", b"BBB")
        batch_id = batch_store.create_batch(
            database, [a, b], identity=batch_store.BatchIdentity()
        )

        hashed = scan_provenance.compute_hashes_for_batch(database, batch_id)
        assert hashed == 2

        availability = scan_provenance.check_availability(database, batch_id)
        assert {item.availability for item in availability} == {
            scan_provenance.ScanAvailability.PRESENT_UNCHANGED
        }

    def test_only_missing_skips_already_hashed_scans(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"AAA")
        batch_id = batch_store.create_batch(
            database, [a], identity=batch_store.BatchIdentity()
        )
        first = scan_provenance.compute_hashes_for_batch(database, batch_id)
        assert first == 1
        second = scan_provenance.compute_hashes_for_batch(database, batch_id, only_missing=True)
        assert second == 0

    def test_a_missing_file_is_skipped_not_raised(self, database, tmp_path: Path) -> None:
        missing = tmp_path / "gone.png"
        batch_id = batch_store.create_batch(
            database, [missing], identity=batch_store.BatchIdentity()
        )
        hashed = scan_provenance.compute_hashes_for_batch(database, batch_id)
        assert hashed == 0

    def test_virtual_sources_are_never_hashed(self, database, tmp_path: Path) -> None:
        virtual = Path("stress:999-000001")
        batch_id = batch_store.create_batch(
            database, [virtual], identity=batch_store.BatchIdentity()
        )
        hashed = scan_provenance.compute_hashes_for_batch(database, batch_id)
        assert hashed == 0


class TestDuplicateGroups:
    def test_identical_content_is_grouped_regardless_of_filename(
        self, database, tmp_path: Path
    ) -> None:
        a = _write(tmp_path / "a.png", b"SAME")
        b = _write(tmp_path / "b.png", b"SAME")
        c = _write(tmp_path / "c.png", b"DIFFERENT")
        batch_id = batch_store.create_batch(
            database, [a, b, c], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)

        groups = scan_provenance.duplicate_groups(database, batch_id)
        assert len(groups) == 1
        assert groups[0].is_exact_duplicate_import
        assert set(groups[0].filenames) == {"a.png", "b.png"}

    def test_no_duplicates_when_every_scan_is_unique(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"AAA")
        b = _write(tmp_path / "b.png", b"BBB")
        batch_id = batch_store.create_batch(
            database, [a, b], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        assert scan_provenance.duplicate_groups(database, batch_id) == ()


class TestCheckAvailability:
    def test_a_changed_file_is_detected(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"ORIGINAL")
        batch_id = batch_store.create_batch(
            database, [a], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)

        a.write_bytes(b"REPLACED CONTENT")
        availability = scan_provenance.check_availability(database, batch_id)
        assert availability[0].availability == scan_provenance.ScanAvailability.CHANGED

    def test_a_missing_file_is_detected(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"ORIGINAL")
        batch_id = batch_store.create_batch(
            database, [a], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        a.unlink()

        availability = scan_provenance.check_availability(database, batch_id)
        assert availability[0].availability == scan_provenance.ScanAvailability.MISSING

    def test_an_unhashed_scan_is_unverifiable(self, database, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.png", b"ORIGINAL")
        batch_id = batch_store.create_batch(
            database, [a], identity=batch_store.BatchIdentity()
        )
        # Deliberately not hashed.
        availability = scan_provenance.check_availability(database, batch_id)
        assert availability[0].availability == scan_provenance.ScanAvailability.UNVERIFIABLE

    def test_virtual_sources_are_excluded(self, database, tmp_path: Path) -> None:
        virtual = Path("stress:1-000001")
        batch_id = batch_store.create_batch(
            database, [virtual], identity=batch_store.BatchIdentity()
        )
        assert scan_provenance.check_availability(database, batch_id) == ()


class TestRelinkScan:
    def test_relinking_to_the_same_content_succeeds(self, database, tmp_path: Path) -> None:
        original = _write(tmp_path / "a.png", b"CONTENT")
        batch_id = batch_store.create_batch(
            database, [original], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[original]

        (tmp_path / "moved").mkdir()
        moved = _write(tmp_path / "moved" / "a.png", b"CONTENT")
        scan_provenance.relink_scan(database, scan_id, moved)

        availability = scan_provenance.check_availability(database, batch_id)
        assert availability[0].source_path == str(moved)
        assert availability[0].availability == scan_provenance.ScanAvailability.PRESENT_UNCHANGED

    def test_relinking_to_different_content_is_refused(self, database, tmp_path: Path) -> None:
        original = _write(tmp_path / "a.png", b"CONTENT")
        batch_id = batch_store.create_batch(
            database, [original], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[original]

        different = _write(tmp_path / "different.png", b"NOT THE SAME SCRIPT")
        with pytest.raises(scan_provenance.RelinkError):
            scan_provenance.relink_scan(database, scan_id, different)

    def test_relinking_to_a_missing_file_is_refused(self, database, tmp_path: Path) -> None:
        original = _write(tmp_path / "a.png", b"CONTENT")
        batch_id = batch_store.create_batch(
            database, [original], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[original]

        with pytest.raises(scan_provenance.RelinkError):
            scan_provenance.relink_scan(database, scan_id, tmp_path / "nowhere.png")

    def test_relinking_an_unhashed_scan_is_refused(self, database, tmp_path: Path) -> None:
        original = _write(tmp_path / "a.png", b"CONTENT")
        batch_id = batch_store.create_batch(
            database, [original], identity=batch_store.BatchIdentity()
        )
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[original]
        # Deliberately not hashed.
        with pytest.raises(scan_provenance.RelinkError):
            scan_provenance.relink_scan(database, scan_id, original)
