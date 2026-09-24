"""Tests for generating images and attendance paperwork as one dataset.

Scope:
    The seam between the two halves - that the candidate population decides
    *identity* while the case plan keeps deciding the *image*, that the sheets
    actually rendered are the ones the population said exist, and that the
    workbooks and ground truth land beside them.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The population decides how many sheets exist.
    B     Identity binding: rolls, sets and their defects reach the sheet.
    C     The paired dataset's files and manifest.
    D     Determinism across the whole paired run.
    ===== ==========================================================

Why these render real images:
    The binding is only meaningful if it survives the renderer. A test that
    checked ``bind_case`` in isolation would pass while the generator ignored
    its result, which is exactly the integration mistake worth guarding.
    Datasets here are kept to a handful of sheets at a low DPI so the suite
    stays quick.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from omr_scanner.evaluation.attendance_dataset import (
    ATTENDANCE_DIRNAME,
    CANDIDATES_FILENAME,
    RECONCILIATION_FILENAME,
    ConflictKind,
    ConflictProfile,
    plan_population,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    ImageFormat,
    generate_dataset,
)
from omr_scanner.services import load_template

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"

SETS = ("10", "11")
SEED = 20260923
DPI = 100
"""Low on purpose: the binding does not depend on resolution, and a full-page
render per sheet is the slow part of this module."""


@pytest.fixture(scope="module")
def template():
    return load_template(TEMPLATE_PATH)


@pytest.fixture(scope="module")
def paired(tmp_path_factory, template):
    """One paired dataset, generated once and inspected by several tests."""
    output = tmp_path_factory.mktemp("paired")
    population = plan_population(count=30, set_codes=SETS, seed=SEED)
    manifest = generate_dataset(
        output,
        template,
        seed=SEED,
        dpi=DPI,
        image_format=ImageFormat.JPEG,
        jpeg_quality=85,
        template_path=str(TEMPLATE_PATH),
        population=population,
    )
    return output, population, manifest


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ----------------------------------------------------------------------
# A - the population decides the sheet count
# ----------------------------------------------------------------------
class TestAPopulationDrivesTheSheets:
    def test_one_image_per_script_the_population_says_exists(self, paired):
        output, population, _ = paired
        images = list((output / IMAGES_DIRNAME).glob("*.jpg"))
        assert len(images) == len(population.sheets_to_render())

    def test_fewer_images_than_candidates(self, paired):
        """Absentees and missing scans are the difference, and are the point."""
        output, population, _ = paired
        images = list((output / IMAGES_DIRNAME).glob("*.jpg"))
        assert len(images) < len(population.candidates)

    def test_every_image_has_its_own_file(self, paired):
        """A duplicate *roll* is intended; an overwritten image is not."""
        output, _, _ = paired
        images = list((output / IMAGES_DIRNAME).glob("*.jpg"))
        assert len({path.name for path in images}) == len(images)

    def test_a_population_with_no_scripts_is_refused(self, tmp_path, template):
        """Better than writing an empty dataset that looks like a success."""
        population = plan_population(
            count=4,
            set_codes=SETS,
            seed=SEED,
            rates=replace(ConflictProfile.NONE.rates(), true_absentee=1.0),
            include_edge_cases=False,
        )
        with pytest.raises(ValueError, match="no scripts at all"):
            generate_dataset(
                tmp_path, template, dpi=DPI, population=population
            )


# ----------------------------------------------------------------------
# B - identity binding survives the renderer
# ----------------------------------------------------------------------
class TestBIdentityReachesTheSheets:
    def _truths(self, output: Path) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((output / GROUND_TRUTH_DIRNAME).glob("*.json"))
        ]

    def test_the_rendered_rolls_are_the_population_s_rolls(self, paired):
        output, population, _ = paired
        rendered = {truth.get("roll", "") for truth in self._truths(output)}
        expected = {sheet.observed_roll or "" for sheet in population.sheets_to_render()}
        assert rendered == expected

    def test_an_unresolved_identifier_renders_with_no_effective_roll(self, paired):
        """Blank, partial and over-marked all leave the roll unusable."""
        output, population, _ = paired
        unusable = sum(
            1
            for sheet in population.sheets_to_render()
            if sheet.observed_roll is None
        )
        blank = sum(1 for truth in self._truths(output) if not truth.get("roll"))
        assert blank == unusable
        assert unusable >= 3  # blank, partial and multiple-mark are each staged

    def test_both_duplicate_scripts_are_rendered_with_the_same_roll(self, paired):
        output, population, _ = paired
        duplicate = next(
            candidate
            for candidate in population.candidates
            if candidate.conflict is ConflictKind.DUPLICATE_SCRIPT
        )
        carrying = [
            truth
            for truth in self._truths(output)
            if truth.get("roll") == duplicate.roll
        ]
        assert len(carrying) == 2

    def test_the_unknown_identifier_is_rendered(self, paired):
        output, population, _ = paired
        unknown = next(
            candidate
            for candidate in population.candidates
            if candidate.conflict is ConflictKind.UNKNOWN_CANDIDATE_ID
        )
        rolls = {truth.get("roll") for truth in self._truths(output)}
        assert unknown.observed_roll in rolls

    def test_a_wrong_identifier_is_not_one_of_the_registered_rolls(self, paired):
        _, population, _ = paired
        registered = {candidate.roll for candidate in population.candidates}
        strays = [
            sheet
            for sheet in population.stray_sheets
            if sheet.conflict is ConflictKind.WRONG_CANDIDATE_ID
        ]
        assert strays
        for stray in strays:
            assert stray.observed_roll not in registered

    def test_the_bound_case_rewrites_the_marks_that_get_drawn(self, template):
        """The regression this class exists for.

        The renderer draws from ``case.marks``; ``roll_marks`` only reaches the
        ground truth. An earlier version of ``bind_case`` set the second and
        not the first, so every sheet carried its originally planned roll while
        the truth file claimed the population's. Every test in this file
        compared metadata with metadata and passed throughout.
        """
        from omr_scanner.evaluation.attendance_dataset import bind_case
        from omr_scanner.evaluation.case_plans import plan_dataset
        from omr_scanner.evaluation.test_cases import FieldLayout

        layout = FieldLayout.of(template)
        population = plan_population(count=30, set_codes=SETS, seed=SEED)
        case = plan_dataset(template, count=1, seed=SEED)[0]

        blank = next(
            sheet
            for sheet in population.sheets_to_render()
            if sheet.conflict is ConflictKind.BLANK_CANDIDATE_ID
        )
        bound = bind_case(case, blank, layout)
        drawn = bound.marks[layout.identifier_zone]
        assert drawn, "the identifier zone must still be present, just unmarked"
        assert all(not plan.labels for plan in drawn.values())

        normal = next(
            sheet
            for sheet in population.sheets_to_render()
            if sheet.conflict is ConflictKind.NONE
        )
        bound_normal = bind_case(case, normal, layout)
        marked = bound_normal.marks[layout.identifier_zone]
        assert [plan.labels for plan in marked.values()] == [
            (digit,) for digit in normal.roll
        ]

    def test_an_unmarked_identifier_really_has_less_ink_on_the_page(self, paired):
        """Measured on the pixels, because that is where the bug was.

        A blank identifier must render fewer filled bubbles than a complete
        one. Compared as an inequality over the same grid region rather than
        against an absolute count, so the assertion survives a change of DPI,
        template or mark style.
        """
        import numpy as np
        from PIL import Image

        output, _, _ = paired

        def roll_ink(scan: str) -> int:
            page = np.array(Image.open(output / IMAGES_DIRNAME / scan).convert("L"))
            height, width = page.shape
            grid = page[
                int(height * 0.26) : int(height * 0.44),
                int(width * 0.09) : int(width * 0.30),
            ]
            return int((grid < 100).sum())

        truths = {
            json.loads(path.read_text(encoding="utf-8"))["scan"]: json.loads(
                path.read_text(encoding="utf-8")
            )
            for path in (output / GROUND_TRUTH_DIRNAME).glob("*.json")
        }

        def scan_for(kind: ConflictKind) -> str:
            return next(
                scan
                for scan, truth in truths.items()
                if (truth.get("notes") or "").endswith(kind.value)
            )

        blank = roll_ink(scan_for(ConflictKind.BLANK_CANDIDATE_ID))
        complete = roll_ink(scan_for(ConflictKind.NONE))
        partial = roll_ink(scan_for(ConflictKind.PARTIAL_CANDIDATE_ID))
        assert blank < partial < complete

    def test_the_set_codes_rendered_are_the_ones_planned(self, paired):
        output, population, _ = paired
        rendered = {truth.get("set_code", "") for truth in self._truths(output)}
        expected = {sheet.observed_set or "" for sheet in population.sheets_to_render()}
        assert rendered == expected


# ----------------------------------------------------------------------
# C - the dataset on disk
# ----------------------------------------------------------------------
class TestCTheDatasetOnDisk:
    def test_one_workbook_per_set_beside_the_images(self, paired):
        output, _, _ = paired
        books = sorted((output / ATTENDANCE_DIRNAME).glob("*.xlsx"))
        assert [path.name for path in books] == [
            f"Set_{code}_Attendance.xlsx" for code in SETS
        ]

    def test_the_reconciliation_ground_truth_is_written(self, paired):
        output, population, _ = paired
        truths = output / GROUND_TRUTH_DIRNAME
        candidates = read_csv(truths / CANDIDATES_FILENAME)
        reconciliation = read_csv(truths / RECONCILIATION_FILENAME)
        assert len(candidates) == len(population.candidates)
        assert len(reconciliation) == len(population.candidates) + len(
            population.stray_sheets
        )

    def test_the_manifest_records_the_attendance_composition(self, paired):
        """So a dataset stays traceable to the cohort it was generated for."""
        _, population, manifest = paired
        attendance = manifest.generator["attendance"]
        assert attendance["registered_candidates"] == len(population.candidates)
        assert attendance["seed"] == SEED
        assert sorted(attendance["sets"]) == list(SETS)
        assert attendance["expected_conflicts"][ConflictKind.DUPLICATE_SCRIPT.value] >= 1

    def test_the_workbooks_import_through_omrflow_s_own_parser(self, paired):
        """Generation is not finished if the application cannot read the result."""
        from omr_scanner.services.candidate_import import read_roster

        output, population, _ = paired
        grouped = population.by_set()
        for path in sorted((output / ATTENDANCE_DIRNAME).glob("*.xlsx")):
            code = path.stem.split("_")[1]
            result = read_roster(path)
            assert result.issues == ()
            assert len(result.candidates) == len(grouped[code])

    def test_a_dataset_without_a_population_writes_no_attendance(
        self, tmp_path, template
    ):
        """The image-only dataset keeps behaving exactly as it did."""
        generate_dataset(
            tmp_path, template, count=3, dpi=DPI, seed=SEED, population=None
        )
        assert not (tmp_path / ATTENDANCE_DIRNAME).exists()
        assert len(list((tmp_path / IMAGES_DIRNAME).glob("*"))) == 3


# ----------------------------------------------------------------------
# D - determinism of the whole paired run
# ----------------------------------------------------------------------
class TestDDeterminism:
    def test_the_same_seed_reproduces_the_same_paired_dataset(
        self, tmp_path, template
    ):
        outputs = []
        for run in ("first", "second"):
            directory = tmp_path / run
            population = plan_population(count=20, set_codes=SETS, seed=SEED)
            generate_dataset(
                directory,
                template,
                seed=SEED,
                dpi=DPI,
                template_path=str(TEMPLATE_PATH),
                population=population,
            )
            outputs.append(directory)

        first, second = outputs
        for name in (CANDIDATES_FILENAME, RECONCILIATION_FILENAME):
            assert (first / GROUND_TRUTH_DIRNAME / name).read_bytes() == (
                second / GROUND_TRUTH_DIRNAME / name
            ).read_bytes()

        # And the images themselves, which is the stronger claim: the same
        # request twice is the same dataset byte for byte, not merely the same
        # answer key.
        first_images = sorted((first / IMAGES_DIRNAME).iterdir())
        second_images = sorted((second / IMAGES_DIRNAME).iterdir())
        assert [p.name for p in first_images] == [p.name for p in second_images]
        for left, right in zip(first_images, second_images, strict=True):
            assert left.read_bytes() == right.read_bytes()
