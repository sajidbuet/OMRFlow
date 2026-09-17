"""Tests for turning a user's file selection into an ordered scan list."""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.services.scan_import import (
    SUPPORTED_SCAN_SUFFIXES,
    collect_scan_files,
    is_supported_scan,
    natural_sort_key,
)


def touch(directory: Path, *names: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for name in names:
        path = directory / name
        path.write_bytes(b"not really an image")
        made.append(path)
    return made


class TestSupportedFormats:
    @pytest.mark.parametrize(
        "name",
        ["a.png", "a.jpg", "a.jpeg", "a.tif", "a.tiff", "a.bmp"],
    )
    def test_the_documented_formats_are_accepted(self, name):
        assert is_supported_scan(Path(name))

    @pytest.mark.parametrize("name", ["A.PNG", "a.JpEg", "a.TIFF"])
    def test_the_extension_is_matched_case_insensitively(self, name):
        assert is_supported_scan(Path(name))

    @pytest.mark.parametrize(
        "name", ["a.pdf", "a.gif", "a.webp", "a.txt", "a.omrt", "a", "a.png.bak"]
    )
    def test_everything_else_is_rejected(self, name):
        assert not is_supported_scan(Path(name))

    def test_pdf_is_deliberately_not_supported(self):
        # Reading PDF would mean adding a rasteriser to the runtime
        # dependencies; the roadmap defers it rather than smuggling it in.
        assert ".pdf" not in SUPPORTED_SCAN_SUFFIXES


class TestNaturalSort:
    def test_ten_sorts_after_two(self, tmp_path: Path):
        files = touch(tmp_path, "scan10.png", "scan2.png", "scan1.png")
        assert [p.name for p in sorted(files, key=natural_sort_key)] == [
            "scan1.png",
            "scan2.png",
            "scan10.png",
        ]

    def test_a_long_run_stays_in_numeric_order(self, tmp_path: Path):
        names = [f"scan{index}.png" for index in range(1, 25)]
        files = touch(tmp_path, *reversed(names))
        assert [p.name for p in sorted(files, key=natural_sort_key)] == names

    def test_case_does_not_split_a_series(self, tmp_path: Path):
        files = touch(tmp_path, "IMG_2.png", "img_10.png", "Img_1.png")
        assert [p.name for p in sorted(files, key=natural_sort_key)] == [
            "Img_1.png",
            "IMG_2.png",
            "img_10.png",
        ]

    def test_zero_padded_and_bare_numbers_interleave_correctly(self, tmp_path: Path):
        files = touch(tmp_path, "s9.png", "s010.png", "s11.png")
        assert [p.name for p in sorted(files, key=natural_sort_key)] == [
            "s9.png",
            "s010.png",
            "s11.png",
        ]

    def test_multiple_number_runs_are_compared_left_to_right(self, tmp_path: Path):
        files = touch(tmp_path, "b2_p10.png", "b2_p2.png", "b10_p1.png")
        assert [p.name for p in sorted(files, key=natural_sort_key)] == [
            "b2_p2.png",
            "b2_p10.png",
            "b10_p1.png",
        ]

    def test_files_stay_grouped_by_folder(self, tmp_path: Path):
        first = touch(tmp_path / "batch_a", "scan2.png", "scan10.png")
        second = touch(tmp_path / "batch_b", "scan1.png")
        ordered = sorted(first + second, key=natural_sort_key)
        assert [p.parent.name for p in ordered] == ["batch_a", "batch_a", "batch_b"]


class TestCollectScanFiles:
    def test_a_plain_selection_of_files_is_ordered_naturally(self, tmp_path: Path):
        files = touch(tmp_path, "scan10.png", "scan1.png", "scan2.png")
        assert [p.name for p in collect_scan_files(files)] == [
            "scan1.png",
            "scan2.png",
            "scan10.png",
        ]

    def test_a_folder_contributes_the_images_it_contains(self, tmp_path: Path):
        touch(tmp_path / "scans", "a.png", "b.jpg", "notes.txt", "key.omrt")
        collected = collect_scan_files([tmp_path / "scans"])
        assert [p.name for p in collected] == ["a.png", "b.jpg"]

    def test_a_folder_does_not_descend_by_default(self, tmp_path: Path):
        touch(tmp_path / "scans", "top.png")
        touch(tmp_path / "scans" / "nested", "deep.png")
        assert [p.name for p in collect_scan_files([tmp_path / "scans"])] == ["top.png"]

    def test_descending_can_be_requested(self, tmp_path: Path):
        touch(tmp_path / "scans", "top.png")
        touch(tmp_path / "scans" / "nested", "deep.png")
        collected = collect_scan_files([tmp_path / "scans"], recursive=True)
        assert {p.name for p in collected} == {"top.png", "deep.png"}

    def test_files_and_folders_can_be_mixed(self, tmp_path: Path):
        loose = touch(tmp_path, "loose.png")
        touch(tmp_path / "scans", "a.png")
        collected = collect_scan_files([*loose, tmp_path / "scans"])
        assert {p.name for p in collected} == {"loose.png", "a.png"}

    def test_the_same_file_selected_twice_appears_once(self, tmp_path: Path):
        files = touch(tmp_path, "a.png")
        collected = collect_scan_files([files[0], files[0], tmp_path])
        assert len(collected) == 1

    def test_a_file_reached_by_two_different_paths_appears_once(self, tmp_path: Path):
        touch(tmp_path / "scans", "a.png")
        direct = tmp_path / "scans" / "a.png"
        indirect = tmp_path / "scans" / "." / "a.png"
        assert len(collect_scan_files([direct, indirect])) == 1

    def test_a_path_that_does_not_exist_is_skipped_rather_than_raising(
        self, tmp_path: Path
    ):
        real = touch(tmp_path, "a.png")
        collected = collect_scan_files([tmp_path / "gone.png", *real])
        assert [p.name for p in collected] == ["a.png"]

    def test_an_empty_selection_collects_nothing(self):
        assert collect_scan_files([]) == ()

    def test_an_empty_folder_collects_nothing(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        assert collect_scan_files([tmp_path / "empty"]) == ()
