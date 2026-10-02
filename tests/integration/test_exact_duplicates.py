"""Exact-content duplicate images at registration, session-wide (0.1.1 phase 4).

Scope:
    :func:`omr_scanner.services.scan_lifecycle.link_exact_duplicates` - the
    last step of registering a run's files, after their content hashes are
    recorded and before anything is read. Rows are registered unread (as the
    Scan stage registers them) over real files, so the hashes are real.

    The recognition-cost half (a duplicate is never read) is proved through
    the real Scan stage in ``tests/gui/test_exact_duplicates_gui.py``.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from tests.integration.test_reject_and_rescan import OPERATOR, TEMPLATE, build_world, reject
from tests.integration.test_session_population import SessionWorld

from omr_scanner.database.models import AuditEvent, BatchScan, ScanBatch, ScanRejection
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    report_store,
    scan_lifecycle,
    scan_provenance,
    scan_sessions,
    session_population,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Exact Duplicates")
    try:
        yield SessionWorld(build_world(session, tmp_path))
    finally:
        if not session.is_closed:
            session.close()


def register(
    sw: SessionWorld,
    files: list[tuple[str, bytes]],
    *,
    folder: str = "manual",
    scan_session_id: str | None = None,
    batch_id: str | None = None,
    link: bool = True,
) -> str:
    """Register files unread - as *Process All* does - hash them, then link duplicates."""
    directory = sw.world.scans_dir / folder
    directory.mkdir(parents=True, exist_ok=True)
    sw._clock += timedelta(seconds=1)  # later than every batch so far, as in use
    new = batch_id is None
    batch = batch_id or batch_store.new_batch_id()
    with sw.database.session() as session:
        if new:
            session.add(
                ScanBatch(
                    batch_id=batch, created_at=sw._clock, updated_at=sw._clock,
                    source_folder=str(directory), status="running", total_scans=len(files),
                    scan_session_id=scan_session_id or sw.scan_session_id,
                )
            )
            session.flush()
        start = int(
            session.scalar(
                select(func.count()).select_from(BatchScan).where(BatchScan.batch_id == batch)
            )
            or 0
        )
        for offset, (name, content) in enumerate(files):
            path = directory / name
            path.write_bytes(content)
            session.add(
                BatchScan(
                    batch_id=batch, batch_index=start + offset, source_path=str(path),
                    filename=name, status="pending",
                )
            )
    if new and scan_session_id is None:
        sw.batches.append(batch)
    scan_provenance.compute_hashes_for_batch(sw.database, batch)
    for path, scan_id in batch_store.scan_ids_by_path(sw.database, batch).items():
        sw.ids[f"{folder}/{path.name}"] = scan_id
    if link:
        scan_lifecycle.link_exact_duplicates(sw.database, batch)
    return batch


def state(sw: SessionWorld, key: str) -> tuple[str, str, int | None]:
    """``(batch_scan.status, lifecycle state, scan it repeats)`` of one sheet."""
    scan_id = sw.ids[key]
    with sw.database.session() as session:
        status = session.scalar(select(BatchScan.status).where(BatchScan.scan_id == scan_id))
        row = session.scalars(
            select(ScanRejection).where(ScanRejection.scan_id == scan_id)
        ).one_or_none()
    return (
        str(status),
        row.state if row is not None else "active",
        row.reimport_of_scan_id if row is not None else None,
    )


ORIGINAL = b"scan:s1a.png"  # build_world's bytes for s1a.png (batch 0)


class TestExactDuplicates:
    def test_1_same_bytes_different_name_same_batch(self, sw):
        register(sw, [("a.tif", b"same"), ("b.tif", b"same")])
        assert state(sw, "manual/a.tif") == ("pending", "active", None)
        assert state(sw, "manual/b.tif") == (
            "duplicate", "duplicate_content", sw.ids["manual/a.tif"]
        )

    def test_2_same_bytes_in_another_batch_of_the_session(self, sw):
        register(sw, [("copy_of_s1a.tif", ORIGINAL)])
        assert state(sw, "manual/copy_of_s1a.tif") == (
            "duplicate", "duplicate_content", sw.ids["s1a.png"]
        )

    def test_3_same_bytes_through_two_manual_folders(self, sw):
        batch = register(sw, [("x.tif", b"paper-1")], folder="desk_a")
        register(sw, [("y.tif", b"paper-1")], folder="desk_b", batch_id=batch)
        assert state(sw, "desk_a/x.tif")[0] == "pending"
        assert state(sw, "desk_b/y.tif")[1] == "duplicate_content"

    def test_4_same_name_different_bytes_is_not_a_duplicate(self, sw):
        batch = register(sw, [("x.tif", b"paper-1")], folder="desk_a")
        register(sw, [("x.tif", b"paper-2")], folder="desk_b", batch_id=batch)
        assert state(sw, "desk_a/x.tif") == ("pending", "active", None)
        assert state(sw, "desk_b/x.tif") == ("pending", "active", None)

    def test_5_another_session_is_not_collapsed_into_this_one(self, sw):
        other = scan_sessions.create_scan_session(sw.database, name="Other", activate=False)
        register(sw, [("o.tif", ORIGINAL)], folder="other", scan_session_id=other.scan_session_id)
        assert state(sw, "other/o.tif") == ("pending", "active", None)
        assert session_population.session_of_batch(
            sw.database, sw.batches[0]
        ) != other.scan_session_id

    def test_6_linking_again_and_reopening_change_nothing(self, sw):
        batch = register(sw, [("a.tif", b"same"), ("b.tif", b"same")])
        with sw.database.session() as session:
            events = session.scalar(select(func.count()).select_from(AuditEvent))
        assert scan_lifecycle.link_exact_duplicates(sw.database, batch) == ()
        root = sw.world.session.root
        sw.world.session.close()
        with open_project(root) as reopened:
            assert scan_lifecycle.link_exact_duplicates(reopened.database, batch) == ()
            with reopened.database.session() as session:
                assert session.scalar(select(func.count()).select_from(AuditEvent)) == events
            assert scan_lifecycle.duplicate_images(reopened.database, batch)

    def test_7_a_copy_of_a_rejected_sheet_is_a_re_import_and_not_read(self, sw):
        reject(sw.world, "s1a.png")
        register(sw, [("again.tif", ORIGINAL)])
        assert state(sw, "manual/again.tif") == (
            "duplicate", "reimport_of_rejected", sw.ids["s1a.png"]
        )

    def test_7b_a_copy_of_a_superseded_sheet_stays_out(self, sw):
        reject(sw.world, "s1a.png")
        later = register(sw, [("rescan.tif", b"a genuine rescan")])
        # The rescan is read (recognition is not under test here).
        with sw.database.session() as session:
            session.execute(
                BatchScan.__table__.update()
                .where(BatchScan.scan_id == sw.ids["manual/rescan.tif"])
                .values(status="completed", identifier_value="100121", set_code_value="1")
            )
        scan_lifecycle.confirm_replacement(
            sw.database, sw.ids["s1a.png"], sw.ids["manual/rescan.tif"], reviewer=OPERATOR
        )
        register(sw, [("old_again.tif", ORIGINAL)], batch_id=later)
        assert state(sw, "manual/old_again.tif")[:2] == ("duplicate", "reimport_of_rejected")

    def test_7c_a_copy_of_an_excluded_sheet_does_not_sneak_back(self, sw):
        scan_lifecycle.exclude_scan(
            sw.database, sw.ids["s1a.png"], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
        register(sw, [("again.tif", ORIGINAL)])
        assert state(sw, "manual/again.tif")[1] == "duplicate_content"

    def test_8_a_duplicate_is_never_a_second_effective_script(self, sw):
        register(sw, [("copy_of_s1a.tif", ORIGINAL)])
        population = sw.population()
        copy = sw.ids["manual/copy_of_s1a.tif"]
        assert population.dispositions[copy] is SheetDisposition.EXACT_DUPLICATE
        assert copy not in population.effective and copy not in population.reconciled
        assert sw.ids["s1a.png"] in population.effective
        # Not an unread sheet either: a Final Export is not held up by it.
        report = report_store.check_readiness(
            sw.database, sw.world.rosters["1"], sw.batches[0], TEMPLATE, "1",
            for_final_export=True,
        )
        assert not any("not been read yet" in item.message for item in report.issues)
        assert scan_lifecycle.count_cases(
            sw.database, sw.batches[0], session_wide=True
        ).duplicates == 1

    def test_9_the_link_is_audited_and_kept(self, sw):
        register(sw, [("copy_of_s1a.tif", ORIGINAL)])
        copy = sw.ids["manual/copy_of_s1a.tif"]
        history = scan_lifecycle.lifecycle_history(sw.database, copy)
        assert [item.action for item in history] == ["duplicate_linked"]
        assert str(sw.ids["s1a.png"]) in history[0].detail
        cases = scan_lifecycle.list_cases(
            sw.database, sw.batches[0], include_reimports=True, session_wide=True
        )
        assert copy in {item.scan_id for item in cases}

    def test_a_reprocess_batch_is_not_a_duplicate_of_the_batch_it_replaces(self, sw):
        first = register(sw, [("p.tif", b"paper-9")])
        with sw.database.session() as session:
            session.execute(
                ScanBatch.__table__.update()
                .where(ScanBatch.batch_id == first)
                .values(sealed_at=datetime.now(UTC), status="completed")
            )
        # Reprocess All registers the same files into a new batch and records
        # the supersession before the run; then the run's registration step.
        second = register(sw, [("p_again.tif", b"paper-9")], folder="reprocess", link=False)
        scan_sessions.record_supersession(sw.database, first, second, reason="reprocess")
        assert scan_lifecycle.link_exact_duplicates(sw.database, second) == ()
        assert state(sw, "reprocess/p_again.tif") == ("pending", "active", None)
        assert state(sw, "manual/p.tif") == ("pending", "active", None)
