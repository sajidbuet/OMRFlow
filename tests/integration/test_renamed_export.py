"""Renamed copies of a multi-batch session's effective sheets (ROADMAP Phase C scope).

On the acceptance examination (three batches, rescans, duplicates, a corrected
and an unresolved Student ID): exactly the effective set is exported, named by
the effective (reviewed) Student ID, in session order, deterministically, and
nothing is ever overwritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.integration.test_session_acceptance import (
    Exam,
    build,
    expected_effective,
    roll,
    sheet,
)

from omr_scanner.services import create_project
from omr_scanner.services.renamed_export import export_renamed_copies

if TYPE_CHECKING:
    from pathlib import Path


def test_the_effective_set_is_exported_under_reviewed_names(workspace: Path, tmp_path: Path):
    session = create_project(workspace, "Renamed Export")
    try:
        exam = Exam(session, tmp_path)
        build(exam)
        exported = export_renamed_copies(exam.database, exam.scan_session_id, tmp_path / "out")
        by_scan = {item.scan_id: item.name for item in exported.copies}
        assert set(by_scan) == expected_effective(exam)
        assert exported.missing_sources == ()
        # The rescans carry the candidate's name; the originals they replace are absent.
        assert by_scan[exam.ids["R1_100003.tif"]] == f"{roll('1', 3)}.tif"
        assert by_scan[exam.ids["G2_200004.tif"]] == f"{roll('2', 4)}.tif"
        assert exam.ids[sheet("1", 3)] not in by_scan
        # The reviewer's correction names the file, not the machine's reading.
        assert by_scan[exam.ids[sheet("3", 30)]] == f"{roll('3', 30)}.tif"
        # A duplicate ID within a batch: both copies, the later one suffixed.
        assert {by_scan[exam.ids[sheet("1", 13)]], by_scan[exam.ids["dupA.tif"]]} == {
            f"{roll('1', 13)}.tif", f"{roll('1', 13)}_a.tif",
        }
        # An unresolved identity is never named after a value nobody trusts.
        assert by_scan[exam.ids["blur.tif"]].startswith("UNRESOLVED_")
        assert exported.unresolved == 1
        written = sorted(path.name for path in (tmp_path / "out").iterdir())
        assert written == sorted(by_scan.values())

        again = export_renamed_copies(exam.database, exam.scan_session_id, tmp_path / "again")
        assert [item.name for item in again.copies] == [item.name for item in exported.copies]

        before = {path.name: path.read_bytes() for path in (tmp_path / "out").iterdir()}
        export_renamed_copies(exam.database, exam.scan_session_id, tmp_path / "out")
        for name, content in before.items():
            assert (tmp_path / "out" / name).read_bytes() == content  # never overwritten
        assert len(list((tmp_path / "out").iterdir())) == 2 * len(before)
    finally:
        session.close()
