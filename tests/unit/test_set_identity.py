"""Set identity: canonical codes, physical marks, collisions (phase 0.1.1-A).

Covers :mod:`omr_scanner.domain.set_identity` and the validation rules
:mod:`omr_scanner.domain.exam_sets` applies with it. Pure: no database.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.exam_sets import (
    ExamSet,
    find_conflicting_mark,
    find_conflicting_set,
    validate_physical_mark,
)
from omr_scanner.domain.set_identity import (
    SetCodeMap,
    SetIdentity,
    canonical_code,
    distinct_codes,
    find_collisions,
    group_by_set,
    same_set,
)

FULL_WIDTH_A = chr(0xFF21)
"""FULLWIDTH LATIN CAPITAL LETTER A - NFKC folds it to ``A``."""
FULL_WIDTH_1 = chr(0xFF11)
"""FULLWIDTH DIGIT ONE - NFKC folds it to ``1``."""


def _set(set_id: str, code: str, mark: str = "", order: int = 0) -> ExamSet:
    return ExamSet(set_id=set_id, code=code, display_order=order, physical_mark=mark)


class TestCanonicalCode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("A", "A"),
            ("a", "A"),
            (" A ", "A"),
            ("\ta\n", "A"),
            (FULL_WIDTH_A, "A"),
            (f"{FULL_WIDTH_1}0", "10"),
            ("10", "10"),
            ("x1", "X1"),
            ("EEE-01", "EEE-01"),
        ],
    )
    def test_the_canonical_form(self, raw: str, expected: str) -> None:
        assert canonical_code(raw) == expected

    def test_case_whitespace_and_width_name_one_set(self) -> None:
        assert same_set("A", "a")
        assert same_set("A", " A ")
        assert same_set("A", FULL_WIDTH_A)
        assert same_set("10", "10")

    def test_a_leading_zero_is_never_dropped(self) -> None:
        """Set codes are identifiers, not numbers: 05 and 5 are two sets."""
        assert canonical_code("05") == "05"
        assert not same_set("05", "5")
        assert not same_set("010", "10")

    def test_different_codes_stay_different(self) -> None:
        assert not same_set("A", "B")
        assert not same_set("10", "11")

    def test_the_operator_spelling_is_not_changed_by_comparing(self) -> None:
        item = _set("s", "a")
        assert item.code == "a"
        assert item.canonical_code == "A"


class TestSetCodeMap:
    def test_lookup_is_canonical(self) -> None:
        found = SetCodeMap([("A", 1), ("10", 2)])
        assert found["a"] == 1
        assert found.get(" a ") == 1
        assert "a" in found
        assert found.get("B") is None

    def test_iteration_keeps_the_stored_spelling(self) -> None:
        assert list(SetCodeMap([("a", 1), ("B", 2)])) == ["a", "B"]

    def test_a_canonical_duplicate_is_shadowed_never_silently_overwritten(self) -> None:
        found = SetCodeMap([("A", "first"), ("a", "second")])
        assert found["A"] == "first"
        assert found.shadowed == ("a",)
        assert len(found) == 1

    def test_grouping_and_distinct_codes(self) -> None:
        grouped = group_by_set([("A", 1), ("a", 2), ("B", 3)])
        assert grouped["a"] == [1, 2]
        assert grouped["B"] == [3]
        assert distinct_codes(["A", "a", "B", " b "]) == ("A", "B")


class TestPhysicalMapping:
    def setup_method(self) -> None:
        self.identity = SetIdentity(
            [_set("ten", "10", "A"), _set("eleven", "11", "B"), _set("twelve", "12")]
        )

    def test_logical_to_physical(self) -> None:
        assert self.identity.physical_for_logical("10") == "A"
        assert self.identity.physical_for_logical("11") == "B"

    def test_no_mark_means_the_sheet_prints_the_code(self) -> None:
        assert self.identity.physical_for_logical("12") == "12"

    def test_physical_to_logical(self) -> None:
        assert self.identity.logical_for_physical("A") == "10"
        assert self.identity.logical_for_physical("a") == "10"
        assert self.identity.logical_for_physical("B") == "11"
        assert self.identity.logical_for_physical("12") == "12"

    def test_the_round_trip(self) -> None:
        for code in ("10", "11", "12"):
            printed = self.identity.physical_for_logical(code)
            assert self.identity.logical_for_physical(printed) == code

    def test_a_logical_code_typed_on_the_paper_still_names_its_set(self) -> None:
        assert self.identity.logical_for_physical("10") == "10"

    def test_an_undefined_reading_is_left_exactly_as_read(self) -> None:
        assert self.identity.logical_for_physical("D") == "D"
        assert self.identity.logical_for_physical("_") == "_"
        assert self.identity.for_reading("D") is None

    def test_logical_lookup_does_not_accept_a_mark(self) -> None:
        """A declared (logical) set "A" is not Set 10, even though 10 prints A."""
        assert self.identity.logical("A") is None
        assert self.identity.logical("10") is not None

    def test_display(self) -> None:
        assert self.identity.describe("10") == "Set 10 (A on sheet)"
        assert self.identity.describe("A") == "Set 10 (A on sheet)"
        assert self.identity.describe("12") == "Set 12"
        assert _set("ten", "10", "A").label_with_mark == "Set 10 (A on sheet)"
        assert _set("x", "A").label_with_mark == "Set A"

    def test_unmapped_project(self) -> None:
        identity = SetIdentity([_set("a", "A"), _set("b", "B")])
        assert not identity.has_physical_marks
        assert identity.logical_for_physical("a") == "A"
        assert identity.describe("a") == "Set A"


class TestMarkValidation:
    def test_no_mark(self) -> None:
        assert validate_physical_mark("") == ""
        assert validate_physical_mark("   ") == ""

    def test_a_mark_is_trimmed(self) -> None:
        assert validate_physical_mark(" A ") == "A"

    @pytest.mark.parametrize("bad", ["A\nB", "A\tB", "\x07", "X" * 33])
    def test_unprintable_or_malformed_marks_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="printed set mark"):
            validate_physical_mark(bad)

    def test_two_sets_cannot_print_the_same_mark(self) -> None:
        existing = (_set("ten", "10", "A"),)
        conflict = find_conflicting_mark("A", "11", existing)
        assert conflict is not None and conflict.set_id == "ten"
        assert find_conflicting_mark("a", "11", existing) is not None

    def test_a_mark_may_not_be_another_sets_code(self) -> None:
        existing = (_set("setA", "A"),)
        conflict = find_conflicting_mark("A", "10", existing)
        assert conflict is not None and conflict.set_id == "setA"

    def test_a_code_may_not_be_another_sets_mark(self) -> None:
        """Defining Set A when Set 10 is printed as A would make 'A' ambiguous."""
        existing = (_set("ten", "10", "A"),)
        conflict = find_conflicting_set("A", existing)
        assert conflict is not None and conflict.set_id == "ten"

    def test_a_set_may_keep_its_own_mark_when_edited(self) -> None:
        existing = (_set("ten", "10", "A"),)
        assert find_conflicting_mark("A", "10", existing, ignoring="ten") is None

    def test_a_mark_equal_to_its_own_code_is_harmless(self) -> None:
        assert find_conflicting_mark("10", "10", (_set("ten", "10"),)) is None

    def test_defining_lower_case_beside_upper_case_is_refused(self) -> None:
        conflict = find_conflicting_set("a", (_set("A", "A"),))
        assert conflict is not None


class TestCollisions:
    def setup_method(self) -> None:
        self.sets = (_set("upper", "A", order=0), _set("lower", "a", order=1), _set("b", "B"))
        self.identity = SetIdentity(self.sets)

    def test_a_and_lower_case_a_are_reported(self) -> None:
        found = find_collisions(self.sets)
        assert len(found) == 1
        assert found[0].canonical == "A"
        assert [item.set_id for item in found[0].sets] == ["upper", "lower"]
        assert "'A' and 'a'" in found[0].describe()

    def test_nothing_is_merged(self) -> None:
        assert len(self.identity.sets) == 3
        assert self.identity.codes == ("A", "a", "B")

    def test_no_reading_of_the_shared_code_is_given_to_either_set(self) -> None:
        assert self.identity.logical("A") is None
        assert self.identity.logical("a") is None
        assert self.identity.for_reading("A") is None
        assert self.identity.is_ambiguous("a")
        assert self.identity.logical_for_physical("a") == "a"

    def test_other_sets_are_unaffected(self) -> None:
        found = self.identity.logical("b")
        assert found is not None and found.set_id == "b"

    def test_a_clean_project_has_none(self) -> None:
        assert find_collisions((_set("1", "10"), _set("2", "11"))) == ()
