"""Tests for collision-free output file naming.

Why this file is long for such a small module:
    Every duplicate rule here exists to stop one scan overwriting another, and
    an overwritten scan is unrecoverable - the paper has already been filed.
    These are therefore data-loss tests, not formatting tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.services.filename_manager import (
    FilenameAllocator,
    duplicate_suffix,
    normalise_suffix,
    sanitise_stem,
)


class TestDuplicateSuffix:
    def test_the_first_file_has_no_suffix(self):
        assert duplicate_suffix(0) == ""

    @pytest.mark.parametrize(
        ("index", "expected"),
        [(1, "_a"), (2, "_b"), (3, "_c"), (25, "_y"), (26, "_z")],
    )
    def test_the_first_twenty_six_duplicates_use_single_letters(self, index, expected):
        assert duplicate_suffix(index) == expected

    @pytest.mark.parametrize(
        ("index", "expected"),
        [(27, "_aa"), (28, "_ab"), (52, "_az"), (53, "_ba"), (702, "_zz"), (703, "_aaa")],
    )
    def test_beyond_z_it_continues_in_bijective_base_26(self, index, expected):
        assert duplicate_suffix(index) == expected

    def test_every_suffix_up_to_a_thousand_is_unique(self):
        suffixes = [duplicate_suffix(index) for index in range(1000)]
        assert len(set(suffixes)) == len(suffixes)

    def test_a_negative_index_is_rejected(self):
        with pytest.raises(ValueError, match="zero or positive"):
            duplicate_suffix(-1)


class TestSanitiseStem:
    def test_a_plain_roll_number_is_unchanged(self):
        assert sanitise_stem("2103123") == "2103123"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("A*B", "A-B"),
            ("2103/123", "2103-123"),
            ("  2103123  ", "2103123"),
            ("a b", "a-b"),
            ("..2103..", "2103"),
        ],
    )
    def test_unsafe_characters_are_replaced(self, value, expected):
        assert sanitise_stem(value) == expected

    def test_a_value_of_nothing_but_unsafe_characters_becomes_empty(self):
        assert sanitise_stem("***") == "---"
        assert sanitise_stem("   ") == ""

    def test_the_placeholders_recognition_uses_survive_sanitising(self):
        # `?` and `_` are how an unresolved and a blank position render; they
        # must not silently become part of a plausible file name.
        assert sanitise_stem("21?3123") == "21-3123"
        assert sanitise_stem("21_3123") == "21_3123"


class TestNormaliseSuffix:
    @pytest.mark.parametrize(("value", "expected"), [("jpg", ".jpg"), (".png", ".png"), ("", "")])
    def test_a_leading_dot_is_added_when_missing(self, value, expected):
        assert normalise_suffix(value) == expected


class TestAllocationWithoutADirectory:
    def test_a_unique_roll_keeps_its_plain_name(self):
        allocator = FilenameAllocator()
        assert allocator.allocate("2103123", ".jpg") == "2103123.jpg"

    def test_the_extension_is_preserved(self):
        allocator = FilenameAllocator()
        assert allocator.allocate("2103123", ".tiff") == "2103123.tiff"
        assert allocator.allocate("2103124", "png") == "2103124.png"

    def test_the_second_and_third_duplicates_get_a_then_b(self):
        allocator = FilenameAllocator()
        names = [allocator.allocate("2103123", ".jpg") for _ in range(3)]
        assert names == ["2103123.jpg", "2103123_a.jpg", "2103123_b.jpg"]

    def test_duplicates_of_different_rolls_do_not_interfere(self):
        allocator = FilenameAllocator()
        assert allocator.allocate("111", ".jpg") == "111.jpg"
        assert allocator.allocate("222", ".jpg") == "222.jpg"
        assert allocator.allocate("111", ".jpg") == "111_a.jpg"
        assert allocator.allocate("222", ".jpg") == "222_a.jpg"

    def test_the_same_roll_with_different_extensions_does_not_collide(self):
        allocator = FilenameAllocator()
        assert allocator.allocate("2103123", ".jpg") == "2103123.jpg"
        assert allocator.allocate("2103123", ".png") == "2103123.png"

    def test_thirty_duplicates_never_repeat_a_name(self):
        allocator = FilenameAllocator()
        names = [allocator.allocate("2103123", ".jpg") for _ in range(30)]
        assert len(set(names)) == 30
        assert names[26] == "2103123_z.jpg"
        assert names[27] == "2103123_aa.jpg"

    def test_an_unresolved_identifier_gets_a_review_name(self):
        allocator = FilenameAllocator()
        assert allocator.allocate(None, ".jpg") == "UNRESOLVED_001.jpg"
        assert allocator.allocate(None, ".jpg") == "UNRESOLVED_002.jpg"

    def test_an_empty_or_unsafe_identifier_is_treated_as_unresolved(self):
        allocator = FilenameAllocator()
        assert allocator.allocate("", ".jpg") == "UNRESOLVED_001.jpg"
        assert allocator.allocate("   ", ".jpg") == "UNRESOLVED_002.jpg"

    def test_reserving_a_name_makes_the_allocator_avoid_it(self):
        allocator = FilenameAllocator()
        allocator.reserve("2103123.jpg")
        assert allocator.allocate("2103123", ".jpg") == "2103123_a.jpg"


class TestAllocationAgainstADirectory:
    def test_an_existing_file_pushes_the_new_one_to_a(self, tmp_path: Path):
        (tmp_path / "2103123.jpg").write_bytes(b"first")
        allocator = FilenameAllocator(tmp_path)

        assert allocator.allocate("2103123", ".jpg") == "2103123_a.jpg"
        assert (tmp_path / "2103123.jpg").read_bytes() == b"first"

    def test_a_run_of_existing_files_is_skipped_entirely(self, tmp_path: Path):
        for name in ("2103123.jpg", "2103123_a.jpg", "2103123_b.jpg"):
            (tmp_path / name).write_bytes(b"x")
        allocator = FilenameAllocator(tmp_path)

        assert allocator.allocate("2103123", ".jpg") == "2103123_c.jpg"

    def test_existing_unresolved_names_are_skipped_too(self, tmp_path: Path):
        (tmp_path / "UNRESOLVED_001.jpg").write_bytes(b"x")
        allocator = FilenameAllocator(tmp_path)

        assert allocator.allocate(None, ".jpg") == "UNRESOLVED_002.jpg"

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path):
        allocator = FilenameAllocator(tmp_path / "not-created-yet")
        assert allocator.allocate("2103123", ".jpg") == "2103123.jpg"

    def test_case_differences_count_as_collisions_by_default(self, tmp_path: Path):
        # Windows and macOS would have `2103123.JPG` overwrite `2103123.jpg`.
        (tmp_path / "2103123.JPG").write_bytes(b"x")
        allocator = FilenameAllocator(tmp_path)

        assert allocator.allocate("2103123", ".jpg") == "2103123_a.jpg"

    def test_case_sensitivity_can_be_requested(self):
        # Asserted without a real directory on purpose. Whether *the disk* calls
        # `2103123.JPG` and `2103123.jpg` the same file is the file system's
        # decision, not ours, and it differs between Windows and Linux; what the
        # flag deterministically controls is the allocator's own memory.
        insensitive = FilenameAllocator()
        insensitive.reserve("2103123.JPG")
        assert insensitive.allocate("2103123", ".jpg") == "2103123_a.jpg"

        sensitive = FilenameAllocator(case_sensitive=True)
        sensitive.reserve("2103123.JPG")
        assert sensitive.allocate("2103123", ".jpg") == "2103123.jpg"

    def test_nothing_it_allocates_ever_names_an_existing_file(self, tmp_path: Path):
        (tmp_path / "500.jpg").write_bytes(b"x")
        (tmp_path / "500_a.jpg").write_bytes(b"x")
        allocator = FilenameAllocator(tmp_path)

        issued = [allocator.allocate("500", ".jpg") for _ in range(5)]
        # Each issued name is written as it is handed out, exactly as the batch
        # processor does, and none may ever land on a file that already exists.
        for name in issued:
            target = tmp_path / name
            assert not target.exists(), f"{name} would have overwritten an existing file"
            target.write_bytes(b"new")
        assert len(set(issued)) == len(issued)
