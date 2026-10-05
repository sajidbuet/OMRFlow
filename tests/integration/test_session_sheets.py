"""The scan session's paged sheet list and per-source counts (0.1.1 revised phase 8).

:mod:`omr_scanner.services.session_sheets` lists a session's sheets one SQL
page at a time with their provenance; the snapshot's per-source rows now carry
the same partition buckets the session totals use. Real sessions from the
engine rig (fake disk, real recognition).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from tests.engine_rig import EngineRig, readable_sheets
from tests.integration.test_session_finish import BIG, settle
from tests.quality_rig import blank_page

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.services import quality_decisions, scan_lifecycle, session_snapshot
from omr_scanner.services.session_sheets import (
    ConflictStateFilter,
    QualityFilter,
    RescanFilter,
    SheetQuery,
    SheetSort,
    StatusFilter,
    batches_of_source,
    count_sheets,
    list_sheets,
    original_names,
    session_batches,
    session_sources,
    sheet_provenance,
    with_original_names,
)

SHEETS = readable_sheets(12)


@dataclass(frozen=True, slots=True)
class ConflictRecordLike:
    scan_id: int
    scan_name: str


@pytest.fixture
def rig(project_session) -> EngineRig:
    return EngineRig(project_session)


def two_sources(rig: EngineRig) -> tuple[str, str]:
    a, b = rig.source("a"), rig.source("b")
    rig.write("a", [(f"a{i}.png", SHEETS[i]) for i in range(4)])
    rig.write("b", [(f"b{i}.png", SHEETS[i + 4]) for i in range(3)] + [("blank.png", blank_page())])
    rig.new_engine(unit_policy=BIG)
    settle(rig)
    return a, b


class TestListing:
    def test_every_sheet_once_with_source_batch_and_the_original_file_name(self, rig):
        a, b = two_sources(rig)
        rows = list_sheets(rig.database, rig.session_id, limit=100)
        assert len(rows) == count_sheets(rig.database, rig.session_id) == 8
        assert {row.source_label for row in rows} == {"Scanner a", "Scanner b"}
        names = {row.original_name for row in rows}
        assert {"a0.png", "b2.png", "blank.png"} <= names
        # The watched source's sheets are read from the project's own copies:
        # that name is diagnostic detail, never the operator's name for it.
        for row in rows:
            assert row.stored_name != row.original_name
            assert row.batch_label.startswith("Batch ")
            assert row.arrived_at is not None
        batches = session_batches(rig.database, rig.session_id)
        assert [item.position for item in batches] == list(range(1, len(batches) + 1))
        assert {item.source_label for item in batches} == {"Scanner a", "Scanner b"}
        assert {item.source_id for item in session_sources(rig.database, rig.session_id)} == {a, b}

    def test_display_names_are_the_names_the_sheets_arrived_with(self, rig):
        two_sources(rig)
        rows = list_sheets(rig.database, rig.session_id, limit=100)
        names = original_names(rig.database, [row.scan_id for row in rows])
        assert names == {row.scan_id: row.original_name for row in rows}
        records = [ConflictRecordLike(row.scan_id, row.stored_name) for row in rows]
        records.append(ConflictRecordLike(10**9, "unknown.png"))  # not a sheet: unchanged
        shown = with_original_names(rig.database, records, ("scan_id", "scan_name"))
        assert [item.scan_name for item in shown] == [row.original_name for row in rows] + [
            "unknown.png"
        ]

    def test_pages_are_bounded_and_disjoint(self, rig):
        two_sources(rig)
        first = list_sheets(rig.database, rig.session_id, limit=3)
        second = list_sheets(rig.database, rig.session_id, offset=3, limit=3)
        third = list_sheets(rig.database, rig.session_id, offset=6, limit=3)
        assert (len(first), len(second), len(third)) == (3, 3, 2)
        ids = [row.scan_id for row in (*first, *second, *third)]
        assert len(set(ids)) == 8
        # Newest first by default (a live session's arrivals on top).
        assert ids == sorted(ids, reverse=True)

    def test_filters_run_in_sql_and_agree_with_their_counts(self, rig):
        a, b = two_sources(rig)
        database, session_id = rig.database, rig.session_id
        for query in (
            SheetQuery(source_id=a),
            SheetQuery(source_id=b),
            SheetQuery(batch_ids=batches_of_source(database, session_id, a)),
            SheetQuery(status=StatusFilter.READ),
            SheetQuery(status=StatusFilter.NOT_READ),
            SheetQuery(quality=QualityFilter.SUGGESTED),
            SheetQuery(conflict=ConflictStateFilter.UNRESOLVED),
            SheetQuery(conflict=ConflictStateFilter.NONE),
            SheetQuery(search="a1"),
        ):
            rows = list_sheets(database, session_id, query, limit=100)
            assert len(rows) == count_sheets(database, session_id, query), query
        from_a = list_sheets(database, session_id, SheetQuery(source_id=a))
        assert {row.source_id for row in from_a} == {a}
        assert count_sheets(database, session_id, SheetQuery(source_id=a)) == 4
        assert count_sheets(database, session_id, SheetQuery(status=StatusFilter.NOT_READ)) == 0
        suggested = list_sheets(database, session_id, SheetQuery(quality=QualityFilter.SUGGESTED))
        assert [row.original_name for row in suggested] == ["blank.png"]
        assert suggested[0].suggestion_outstanding
        found = list_sheets(database, session_id, SheetQuery(search="a1"))
        assert [row.original_name for row in found] == ["a1.png"]
        unresolved = count_sheets(
            database, session_id, SheetQuery(conflict=ConflictStateFilter.UNRESOLVED)
        )
        none = count_sheets(database, session_id, SheetQuery(conflict=ConflictStateFilter.NONE))
        assert unresolved + none == 8

    def test_rescan_states_and_sorting(self, rig):
        two_sources(rig)
        database, session_id = rig.database, rig.session_id
        blank = quality_decisions.outstanding_suggestions(database, session_id)[0]
        quality_decisions.confirm_suggestion(database, blank.scan_id, reviewer="Operator")
        rejected = list_sheets(database, session_id, SheetQuery(rescan=RescanFilter.REJECTED))
        assert [row.scan_id for row in rejected] == [blank.scan_id]
        assert rejected[0].lifecycle == "rejected_pending_rescan"
        assert not rejected[0].suggestion_outstanding  # answered by the rejection
        assert count_sheets(database, session_id, SheetQuery(rescan=RescanFilter.ACTIVE)) == 7
        by_name = list_sheets(
            database, session_id, SheetQuery(sort=SheetSort.FILE_NAME, descending=False)
        )
        names = [row.original_name for row in by_name]
        assert names == sorted(names)
        other = list_sheets(database, session_id, SheetQuery(rescan=RescanFilter.ACTIVE))[0]
        scan_lifecycle.reject_scan(
            database, other.scan_id, reviewer="Operator", reason=RejectionReason.FOLDED
        )
        assert count_sheets(database, session_id, SheetQuery(rescan=RescanFilter.REJECTED)) == 2

    def test_provenance_of_one_sheet(self, rig):
        two_sources(rig)
        row = list_sheets(rig.database, rig.session_id, SheetQuery(search="b1"))[0]
        found = sheet_provenance(rig.database, row.scan_id)
        assert found is not None
        assert found.source_label == "Scanner b" and found.original_name == "b1.png"
        assert found.batch_label == row.batch_label
        assert "Scanner b" in found.describe() and "b1.png" in found.describe()
        assert sheet_provenance(rig.database, 999_999) is None

    def test_a_reprocessed_watched_sheet_keeps_the_name_it_arrived_with(self, rig):
        """A re-read watched sheet is still named as it arrived (a revised phase 9 finding).

        *Reprocess All* of a watched unit re-reads the project's content-addressed
        copies, which the manual source records under the copy's name.
        """
        from omr_scanner.services import batch_store, scan_recovery, scan_sessions

        a, _b = two_sources(rig)
        database, session_id = rig.database, rig.session_id
        [unit] = batches_of_source(database, session_id, a)
        reprocess = scan_sessions.start_reprocess_batch(
            database, unit, identity=batch_store.BatchIdentity.of(rig.template),
            settings={scan_recovery.WORK_UNIT_SETTING: True}, started_by="Operator",
        )
        assert rig.engine is not None
        rig.engine.shutdown()
        rig.new_engine(unit_policy=BIG)
        rig.run()
        with database.session() as session:
            reread = [row.scan_id for row in session.query(BatchScan)
                      .filter(BatchScan.batch_id == reprocess).all()]
        assert len(reread) == 4
        names = original_names(database, reread, intake_only=True)
        assert sorted(names.values()) == ["a0.png", "a1.png", "a2.png", "a3.png"]
        found = sheet_provenance(database, reread[0])
        assert found is not None and found.original_name in names.values()
        assert found.original_name != found.stored_name
        listed = {row.scan_id: row.original_name for row in list_sheets(
            database, session_id, SheetQuery(search="a2"), limit=100)}
        assert sorted(listed.values()) == ["a2.png", "a2.png"]  # the original and its re-read

    def test_an_empty_session_lists_nothing(self, rig):
        assert list_sheets(rig.database, rig.session_id) == ()
        assert count_sheets(rig.database, rig.session_id) == 0
        assert session_batches(rig.database, rig.session_id) == ()


class TestPerSourceSnapshotCounts:
    def test_per_source_buckets_add_up_to_the_session_totals(self, rig):
        a, b = two_sources(rig)
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        by_source = {item.source_id: item for item in snapshot.sources}
        partition = snapshot.partition
        assert sum(item.accepted for item in by_source.values()) == partition.accepted
        assert sum(item.conflict for item in by_source.values()) == partition.conflict
        assert (
            sum(item.rescan_required for item in by_source.values())
            == partition.rescan_required
        )
        assert by_source[b].rescan_required == 1  # the blank page, at Scanner b
        assert by_source[a].rescan_required == 0
        with rig.database.session() as session:
            from sqlalchemy import func, select

            registered = session.scalar(select(func.count()).select_from(BatchScan))
        assert sum(item.registered for item in by_source.values()) == registered

    def test_a_duplicate_image_counts_at_its_source(self, rig):
        a = rig.source("a")
        rig.write("a", [("one.png", SHEETS[0]), ("copy.png", SHEETS[0])])
        rig.new_engine(unit_policy=BIG)
        settle(rig)
        snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
        source = next(item for item in snapshot.sources if item.source_id == a)
        assert source.duplicate == snapshot.partition.duplicate == 1
