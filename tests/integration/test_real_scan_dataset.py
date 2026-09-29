"""Generating a dataset by laying synthetic marks on a real scanned sheet.

The claim this mode makes is narrow and worth stating exactly: *the page is
real and the marks are not*. Everything here exists to check that claim from
both ends.

    ===== ==========================================================
    Class What it establishes
    ===== ==========================================================
    A     The engine reads a reference-mode sheet exactly as labelled.
    B     Both modes produce the same logical ground truth.
    C     The mode, the colour and the reference are recorded.
    D     Defects the mode cannot produce are removed, not faked.
    E     Colour, grayscale and black-and-white all come out right.
    F     An unusable reference fails the run before it writes anything.
    G     The template mode is exactly what it was.
    H     Attendance and reconciliation survive the change of mode.
    I     The reference is prepared once, and no image is retained.
    ===== ==========================================================

Where the "real" scan comes from here:
    A committed test cannot depend on a photograph nobody has, so the reference
    in these tests is a rendered page put through a plausible scan's worth of
    rotation, noise and uneven lighting. That is enough to exercise every step
    the real thing goes through - decode, register, supersample, warp,
    composite - and it is deliberately *not* a claim that the mode has been
    validated on real paper. The real-paper check is
    ``tests/local/test_real_folded_corner.py``'s neighbour: a local fixture
    that CI cannot have.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.attendance_dataset import (
    ATTENDANCE_DIRNAME,
    CANDIDATES_FILENAME,
    RECONCILIATION_FILENAME,
    plan_population,
)
from omr_scanner.evaluation.case_plans import plan_dataset
from omr_scanner.evaluation.ground_truth import (
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.reference_scan import (
    ReferenceScan,
    ReferenceScanError,
    load_reference_scan,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
    ColorMode,
    DatasetProfile,
    ImageFormat,
    RenderMode,
    TestCaseTag,
    generate_dataset,
    page_render_size,
    render_case,
    sheet_spec_from_template,
    strip_unrenderable_defects,
)
from omr_scanner.imaging.synthetic import DistortionSpec, apply_distortion, render_sheet
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:
    from pathlib import Path

REFERENCE_DPI = 300
"""Resolution the stand-in reference scan is produced at.

Twice the template's canonical 150 dpi, so the supersampling path is the one
actually exercised rather than the trivial one-to-one case."""

REFERENCE_SCAN_CONDITIONS = DistortionSpec(
    rotation_degrees=1.5,
    illumination_gradient=0.15,
    noise_sigma=3.0,
    margin_px=30,
    seed=1234,
)
"""What a real blank sheet arrives looking like: fed slightly crooked, lit
unevenly, with the scanner's own noise on it. Strong enough that registration
has to do real work, mild enough that it is an ordinary office scan and not a
degradation test in disguise."""

ATTENDANCE_ROSTER = 24
"""Candidates in the paired-dataset comparison.

Large enough for ``include_edge_cases`` to fit one of every reconciliation
conflict alongside some ordinary candidates, small enough that rendering the
cohort twice - once per mode - stays a few seconds rather than a minute."""


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def engine():
    return RecognitionEngine(RecognitionOptions(with_preview=False))


@pytest.fixture(scope="module")
def blank_scan(tmp_path_factory, template) -> Path:
    """A stand-in scan of the printed blank form. See the module docstring.

    Module scoped and never modified. Every test here only reads the file, and
    :func:`~omr_scanner.evaluation.reference_scan.render_onto_reference` is
    explicitly proven not to write back into a loaded reference, so rendering
    this page once for the whole module is safe and saves a few dozen renders.
    """
    size = page_render_size(template, REFERENCE_DPI)
    sheet = render_sheet(sheet_spec_from_template(template, {}, render=size))
    scan = apply_distortion(sheet, REFERENCE_SCAN_CONDITIONS).image
    path = tmp_path_factory.mktemp("reference") / "blank_form.png"
    cv2.imwrite(str(path), scan)
    return path


@pytest.fixture(scope="module")
def cohort():
    """A roster carrying one of every deliberate reconciliation conflict."""
    return plan_population(
        count=ATTENDANCE_ROSTER,
        set_codes=("10", "11"),
        seed=4242,
        include_edge_cases=True,
    )


@pytest.fixture(scope="module")
def paired(tmp_path_factory, template, blank_scan: Path, cohort):
    """The same cohort rendered both ways, generated once for the module.

    Module scoped deliberately: this is the only fixture in the file that
    renders two whole datasets, and doing it per test would cost more than
    every other test here put together. Defined at module level rather than as
    a method on the class that uses it, because a class-scoped fixture written
    as an instance method is deprecated and will be removed in pytest 10.
    """
    root = tmp_path_factory.mktemp("attendance_modes")
    drawn, laid = root / "drawn", root / "laid"
    generate(drawn, template, None, population=cohort, write_metadata=True)
    generate(laid, template, blank_scan, population=cohort, write_metadata=True)
    return drawn, laid


def generate(output: Path, template, blank: Path | None, **kwargs: object):
    """Generate a small dataset, in whichever mode the caller asked for.

    ``blank is None`` means the template mode, so a test that wants to compare
    the two writes the same call twice with and without a reference.
    """
    return generate_dataset(
        output,
        template,
        count=kwargs.pop("count", 4),
        seed=kwargs.pop("seed", 606),
        profile=kwargs.pop("profile", DatasetProfile.BASELINE),
        write_metadata=kwargs.pop("write_metadata", False),
        render_mode=(
            RenderMode.REFERENCE_SCAN if blank is not None else RenderMode.TEMPLATE
        ),
        reference_scan=blank,
        **kwargs,
    )


# ----------------------------------------------------------------------
# A. The labels are right
# ----------------------------------------------------------------------
class TestTheEngineReadsWhatWasLabelled:
    """The one test that would make the whole mode worthless if it failed.

    A mark composited a few pixels out of its bubble produces a dataset that
    looks perfect and reads wrong, and no later stage can tell.
    """

    def test_a_clean_sheet_reads_exactly_as_labelled(
        self, tmp_path: Path, template, engine, blank_scan: Path
    ):
        case = plan_dataset(template, count=1, seed=3, profile=DatasetProfile.BASELINE)[0]
        sheet = render_case(
            template,
            case,
            render_mode=RenderMode.REFERENCE_SCAN,
            reference=_load(blank_scan, template),
        )
        path = tmp_path / sheet.truth.scan
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        assert result.identifier_value == sheet.truth.roll
        assert result.set_code_value == sheet.truth.set_code
        for number, expected in sheet.truth.answers.items():
            assert result.answer(number).value == expected, f"question {number}"

    def test_a_whole_generated_dataset_reads_as_labelled(
        self, tmp_path: Path, template, engine, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=4)
        truths = load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME)
        assert truths

        for stem, truth in truths.items():
            result = engine.process(output / IMAGES_DIRNAME / truth.scan, template)
            assert result.identifier_value == truth.roll, stem
            for number, expected in truth.answers.items():
                assert result.answer(number).value == expected, f"{stem} q{number}"

    def test_a_blank_sheet_is_read_as_blank(
        self, tmp_path: Path, template, engine, blank_scan: Path
    ):
        """The converse, and the one a doubled mark layer would break.

        If the mark layer carried the printed option letters as well as the
        marks, every empty bubble would hold two copies of its letter and the
        measured fill would rise. This is what catches that.
        """
        reference = _load(blank_scan, template)
        case = plan_dataset(template, count=1, seed=3, profile=DatasetProfile.BASELINE)[0]
        blanked = replace(case, marks={}, answers=dict.fromkeys(case.answers, ""))
        sheet = render_case(
            template,
            blanked,
            render_mode=RenderMode.REFERENCE_SCAN,
            reference=reference,
        )
        path = tmp_path / "blank_answers.png"
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        for number in blanked.answers:
            assert result.answer(number).value == "", f"question {number}"


# ----------------------------------------------------------------------
# B. The two modes agree about everything except pixels
# ----------------------------------------------------------------------
class TestTheTwoModesShareTheirGroundTruth:
    def test_the_logical_truth_is_identical(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        drawn = tmp_path / "drawn"
        laid = tmp_path / "laid"
        generate(drawn, template, None, count=6, profile=DatasetProfile.RECOGNITION)
        generate(laid, template, blank_scan, count=6, profile=DatasetProfile.RECOGNITION)

        left = load_ground_truth_directory(drawn / GROUND_TRUTH_DIRNAME)
        right = load_ground_truth_directory(laid / GROUND_TRUTH_DIRNAME)
        assert set(left) == set(right)
        for stem in left:
            assert left[stem].roll == right[stem].roll, stem
            assert left[stem].set_code == right[stem].set_code, stem
            assert left[stem].answers == right[stem].answers, stem
            assert left[stem].ambiguous == right[stem].ambiguous, stem

    def test_the_pixels_are_not_identical(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        # Stated explicitly so the test above cannot pass by both modes
        # accidentally doing the same thing.
        drawn = tmp_path / "drawn"
        laid = tmp_path / "laid"
        generate(drawn, template, None, count=1)
        generate(laid, template, blank_scan, count=1)
        first = cv2.imread(str(drawn / IMAGES_DIRNAME / "SYN_000001.png"))
        second = cv2.imread(str(laid / IMAGES_DIRNAME / "SYN_000001.png"))
        assert first.shape != second.shape or not np.array_equal(first, second)

    def test_generation_is_reproducible(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        first = tmp_path / "one"
        second = tmp_path / "two"
        generate(first, template, blank_scan, count=3, seed=99)
        generate(second, template, blank_scan, count=3, seed=99)
        for name in ("SYN_000001.png", "SYN_000002.png", "SYN_000003.png"):
            assert (first / IMAGES_DIRNAME / name).read_bytes() == (
                second / IMAGES_DIRNAME / name
            ).read_bytes(), name


# ----------------------------------------------------------------------
# C. What was done is written down
# ----------------------------------------------------------------------
class TestWhatIsRecorded:
    def test_the_ground_truth_names_the_mode_and_the_reference(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=2)
        truth = next(
            iter(load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME).values())
        )
        assert truth.metadata["render_mode"] == RenderMode.REFERENCE_SCAN.value
        assert truth.metadata["reference_scan"] == "blank_form.png"
        assert truth.metadata["color_mode"] == ColorMode.GRAYSCALE.value
        assert truth.metadata["render"]["derived_from"] == "reference_scan"

    def test_the_resolution_is_recorded_as_not_applicable(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        # A real scan does not declare its DPI in anything readable here, and
        # echoing back the requested one would describe a setting that was
        # never applied.
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=1, dpi=600)
        truth = next(
            iter(load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME).values())
        )
        assert truth.metadata["render"]["dpi"] is None
        assert load_manifest(output / MANIFEST_FILENAME).generator["dpi"] is None

    def test_the_manifest_records_the_registration(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=1)
        generator = load_manifest(output / MANIFEST_FILENAME).generator
        assert generator["render_mode"] == RenderMode.REFERENCE_SCAN.value
        reference = generator["reference_scan"]
        assert reference["name"] == "blank_form.png"
        assert reference["mark_layer_scale"] >= 1
        assert reference["registration"]["max_reprojection_error_px"] < 1.0

    def test_the_caveat_says_which_half_is_real(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        laid = tmp_path / "laid"
        drawn = tmp_path / "drawn"
        generate(laid, template, blank_scan, count=1)
        generate(drawn, template, None, count=1)
        assert "real scanned blank sheet" in load_manifest(
            laid / MANIFEST_FILENAME
        ).notes
        assert "Synthetic data." in load_manifest(drawn / MANIFEST_FILENAME).notes

    def test_the_template_mode_records_itself_too(
        self, tmp_path: Path, template
    ):
        output = tmp_path / "dataset"
        generate(output, template, None, count=1)
        truth = next(
            iter(load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME).values())
        )
        assert truth.metadata["render_mode"] == RenderMode.TEMPLATE.value
        assert truth.metadata["reference_scan"] == ""
        assert truth.metadata["render"]["dpi"] == 150


# ----------------------------------------------------------------------
# D. Defects this mode cannot produce
# ----------------------------------------------------------------------
class TestUnrenderableDefects:
    def test_marker_damage_is_removed_from_the_image_and_the_claim(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(
            output,
            template,
            blank_scan,
            count=40,
            profile=DatasetProfile.DEGRADATION,
        )
        truths = load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME)
        for stem, truth in truths.items():
            assert TestCaseTag.MARKER_MISSING.value not in truth.tags, stem
            assert TestCaseTag.MARKER_DAMAGED.value not in truth.tags, stem
            assert TestCaseTag.ORIENTATION_MISSING.value not in truth.tags, stem
            assert "omitted_markers" not in truth.degradation, stem

    def test_the_same_dataset_drawn_from_the_template_does_carry_them(
        self, tmp_path: Path, template
    ):
        # Guards the test above: the marker cases must really be in this
        # profile, or "none of them are present" would be trivially true.
        output = tmp_path / "dataset"
        generate(output, template, None, count=40, profile=DatasetProfile.DEGRADATION)
        tags = {
            tag
            for truth in load_ground_truth_directory(
                output / GROUND_TRUTH_DIRNAME
            ).values()
            for tag in truth.tags
        }
        assert TestCaseTag.MARKER_MISSING.value in tags

    def test_a_sheet_that_can_no_longer_fail_is_not_expected_to(self, template):
        case = next(
            item
            for item in plan_dataset(
                template, count=40, seed=5, profile=DatasetProfile.DEGRADATION
            )
            if item.omit_markers and item.expect_failure
        )
        stripped = strip_unrenderable_defects(case)
        assert stripped.omit_markers == ()
        assert stripped.expect_failure is False
        assert TestCaseTag.EXPECTED_FAILURE not in stripped.tags
        assert "not applied" in stripped.notes

    def test_a_stripped_sheet_is_never_left_untagged(self, template):
        """A benchmark groups by tag, so an untagged sheet vanishes from it.

        The marker-missing case is tagged only ``MARKER_MISSING`` and
        ``EXPECTED_FAILURE``. Removing the first for being unrenderable and the
        second for no longer being true leaves nothing at all unless something
        puts a tag back.
        """
        case = next(
            item
            for item in plan_dataset(
                template, count=40, seed=5, profile=DatasetProfile.DEGRADATION
            )
            if item.omit_markers and item.expect_failure
        )
        assert strip_unrenderable_defects(case).tags == (TestCaseTag.BASELINE,)

    def test_no_generated_sheet_is_left_untagged(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(
            output, template, blank_scan, count=40, profile=DatasetProfile.DEGRADATION
        )
        for stem, truth in load_ground_truth_directory(
            output / GROUND_TRUTH_DIRNAME
        ).items():
            assert truth.tags, stem

    def test_a_sheet_that_still_has_a_reason_to_fail_keeps_it(self, template):
        # A severely cropped page fails for a reason that has nothing to do
        # with marker damage, so putting the markers back does not make it
        # readable and the expectation must survive.
        case = next(
            item
            for item in plan_dataset(
                template, count=40, seed=5, profile=DatasetProfile.DEGRADATION
            )
            if TestCaseTag.CROP_SEVERE in item.tags
        )
        combined = replace(
            case,
            tags=(*case.tags, TestCaseTag.MARKER_DAMAGED),
            damaged_markers=("top_left",),
        )
        stripped = strip_unrenderable_defects(combined)
        assert stripped.damaged_markers == ()
        assert stripped.expect_failure is True
        assert TestCaseTag.CROP_SEVERE in stripped.tags

    def test_a_case_with_nothing_unrenderable_is_returned_unchanged(self, template):
        case = plan_dataset(template, count=1, seed=1, profile=DatasetProfile.BASELINE)[0]
        assert strip_unrenderable_defects(case) is case


# ----------------------------------------------------------------------
# E. Colour
# ----------------------------------------------------------------------
class TestColourModes:
    @pytest.mark.parametrize(
        ("mode", "expected_channels"),
        [
            (ColorMode.GRAYSCALE, 1),
            (ColorMode.COLOR, 3),
            (ColorMode.BLACK_AND_WHITE, 1),
        ],
    )
    def test_the_written_file_has_the_right_channels(
        self, tmp_path: Path, template, blank_scan: Path, mode, expected_channels
    ):
        output = tmp_path / f"dataset_{mode.value}"
        generate(output, template, blank_scan, count=1, color_mode=mode)
        image = cv2.imread(
            str(output / IMAGES_DIRNAME / "SYN_000001.png"), cv2.IMREAD_UNCHANGED
        )
        channels = 1 if image.ndim == 2 else image.shape[2]
        assert channels == expected_channels

    def test_black_and_white_really_is_two_valued(
        self, tmp_path: Path, template, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(
            output, template, blank_scan, count=1, color_mode=ColorMode.BLACK_AND_WHITE
        )
        image = cv2.imread(
            str(output / IMAGES_DIRNAME / "SYN_000001.png"), cv2.IMREAD_UNCHANGED
        )
        assert set(np.unique(image).tolist()) <= {0, 255}

    def test_colour_works_in_the_template_mode_too(self, tmp_path: Path, template):
        output = tmp_path / "dataset"
        generate(output, template, None, count=1, color_mode=ColorMode.COLOR)
        image = cv2.imread(
            str(output / IMAGES_DIRNAME / "SYN_000001.png"), cv2.IMREAD_UNCHANGED
        )
        assert image.ndim == 3

    def test_a_colour_sheet_still_reads_as_labelled(
        self, tmp_path: Path, template, engine, blank_scan: Path
    ):
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=1, color_mode=ColorMode.COLOR)
        truth = next(
            iter(load_ground_truth_directory(output / GROUND_TRUTH_DIRNAME).values())
        )
        result = engine.process(output / IMAGES_DIRNAME / truth.scan, template)
        assert result.identifier_value == truth.roll
        for number, expected in truth.answers.items():
            assert result.answer(number).value == expected, f"question {number}"

    def test_the_mode_is_recorded(self, tmp_path: Path, template, blank_scan: Path):
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=1, color_mode=ColorMode.COLOR)
        assert (
            load_manifest(output / MANIFEST_FILENAME).generator["color_mode"]
            == ColorMode.COLOR.value
        )


# ----------------------------------------------------------------------
# F. Refusing an unusable reference
# ----------------------------------------------------------------------
class TestRefusal:
    def test_a_missing_reference_is_refused_before_anything_is_written(
        self, tmp_path: Path, template
    ):
        output = tmp_path / "dataset"
        bad = tmp_path / "not_a_scan.png"
        bad.write_bytes(b"nope")
        with pytest.raises(ReferenceScanError):
            generate(output, template, bad, count=5)
        assert not (output / IMAGES_DIRNAME).exists()

    def test_asking_for_the_mode_without_a_scan_is_refused(
        self, tmp_path: Path, template
    ):
        with pytest.raises(ValueError, match="needs a reference scan"):
            generate_dataset(
                tmp_path / "dataset",
                template,
                count=1,
                render_mode=RenderMode.REFERENCE_SCAN,
            )

    def test_a_single_case_refuses_the_same_way(self, template):
        case = plan_dataset(template, count=1, seed=1)[0]
        with pytest.raises(ValueError, match="needs one"):
            render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN)


# ----------------------------------------------------------------------
# G. The original mode is untouched
# ----------------------------------------------------------------------
class TestBackwardsCompatibility:
    def test_the_defaults_are_the_template_mode_in_grayscale(self, template):
        case = plan_dataset(template, count=1, seed=17)[0]
        assert np.array_equal(
            render_case(template, case).image,
            render_case(
                template,
                case,
                render_mode=RenderMode.TEMPLATE,
                color_mode=ColorMode.GRAYSCALE,
            ).image,
        )

    def test_a_template_mode_sheet_is_still_single_channel(self, template):
        case = plan_dataset(template, count=1, seed=17)[0]
        assert render_case(template, case).image.ndim == 2

    def test_a_reference_passed_to_the_template_mode_is_ignored(
        self, template, blank_scan: Path
    ):
        case = plan_dataset(template, count=1, seed=17)[0]
        sheet = render_case(
            template, case, reference=_load(blank_scan, template)
        )
        assert np.array_equal(sheet.image, render_case(template, case).image)
        assert sheet.truth.metadata["reference_scan"] == ""

    def test_the_page_size_still_comes_from_the_dpi(self, template):
        case = plan_dataset(template, count=1, seed=17)[0]
        render = page_render_size(template, 200)
        sheet = render_case(template, case, render=render, image_format=ImageFormat.PNG)
        assert sheet.truth.metadata["render"]["dpi"] == 200
        assert sheet.truth.metadata["render"]["width"] == render.width


# ----------------------------------------------------------------------
# H. Attendance and reconciliation are untouched by the rendering mode
# ----------------------------------------------------------------------
class TestAttendanceSurvivesTheRenderMode:
    """Identity is planned before anything is drawn, and must stay that way.

    The population decides *who exists*, who attended, what each script's roll
    and set code are, and which candidates are deliberately in conflict with
    the paperwork. The rendering mode decides what the pixels look like. These
    are different questions, and the proof that they stayed different is that
    every non-image artefact of a run is **byte-identical** between the two
    modes for the same seed.

    Compared through the CSVs rather than the workbooks: an ``.xlsx`` is a zip
    carrying its own creation timestamp, so two identical rosters produce two
    different files and a byte comparison would fail for a reason that has
    nothing to do with this feature.
    """

    def test_the_workbooks_are_still_written(self, paired):
        _drawn, laid = paired
        workbooks = sorted((laid / ATTENDANCE_DIRNAME).glob("*.xlsx"))
        assert [path.name for path in workbooks] == [
            "Set_10_Attendance.xlsx",
            "Set_11_Attendance.xlsx",
        ]

    def test_the_roster_truth_is_identical(self, paired):
        drawn, laid = paired
        for name in (CANDIDATES_FILENAME, RECONCILIATION_FILENAME):
            assert (drawn / GROUND_TRUTH_DIRNAME / name).read_bytes() == (
                laid / GROUND_TRUTH_DIRNAME / name
            ).read_bytes(), name

    def test_every_deliberate_conflict_is_still_represented(self, paired):
        # Guards the test above: two identical files prove nothing if both are
        # empty of the edge cases the roster was asked to contain.
        _drawn, laid = paired
        text = (laid / GROUND_TRUTH_DIRNAME / RECONCILIATION_FILENAME).read_text(
            encoding="utf-8"
        )
        rows = list(csv.DictReader(text.splitlines()))
        conflicts = {row["conflict_type"] for row in rows} - {"none", ""}
        assert len(conflicts) >= 5, sorted(conflicts)

    def test_identity_on_the_sheets_is_unchanged(self, paired):
        drawn, laid = paired
        left = load_ground_truth_directory(drawn / GROUND_TRUTH_DIRNAME)
        right = load_ground_truth_directory(laid / GROUND_TRUTH_DIRNAME)
        assert set(left) == set(right)
        for stem in left:
            assert left[stem].roll == right[stem].roll, stem
            assert left[stem].set_code == right[stem].set_code, stem
            assert left[stem].duplicate_group == right[stem].duplicate_group, stem

    def test_the_population_still_decides_how_many_images_exist(self, paired, cohort):
        # Absentees and missing scans mean fewer images than candidates. That
        # is the point of the paired dataset, and the rendering mode must not
        # quietly restore the scripts the cohort says do not exist.
        _drawn, laid = paired
        expected = len(cohort.sheets_to_render())
        assert expected < len(cohort.candidates)
        assert len(list((laid / IMAGES_DIRNAME).glob("*.png"))) == expected

    def test_the_manifest_keeps_its_attendance_summary(self, paired, cohort):
        drawn, laid = paired
        left = load_manifest(drawn / MANIFEST_FILENAME).generator["attendance"]
        right = load_manifest(laid / MANIFEST_FILENAME).generator["attendance"]
        assert left == right
        assert right["registered_candidates"] == len(cohort.candidates)
        assert right["sheets_to_render"] == len(cohort.sheets_to_render())

    def test_the_render_mode_is_still_recorded_beside_the_attendance(self, paired):
        _drawn, laid = paired
        generator = load_manifest(laid / MANIFEST_FILENAME).generator
        assert generator["render_mode"] == RenderMode.REFERENCE_SCAN.value
        assert generator["attendance"]["sets"] == ["10", "11"]


# ----------------------------------------------------------------------
# I. One preparation per job, and nothing kept afterwards
# ----------------------------------------------------------------------
class TestTheReferenceIsPreparedOncePerJob:
    """What makes the mode usable on a hundred thousand sheets rather than ten.

    Registering a scan runs the whole alignment pipeline - preprocess, contour
    detection, corner selection, orientation. Doing it per sheet would be
    correct and unusably slow, and nothing in the output would reveal it: the
    images would be identical. So it is asserted directly.
    """

    def test_the_scan_is_aligned_exactly_once(
        self, tmp_path: Path, template, blank_scan: Path, monkeypatch
    ):
        from omr_scanner.evaluation import reference_scan as module

        real = module.align_sheet
        calls: list[int] = []

        def counting(image: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return real(image, **kwargs)

        monkeypatch.setattr(module, "align_sheet", counting)
        output = tmp_path / "dataset"
        generate(output, template, blank_scan, count=8)

        assert len(list((output / IMAGES_DIRNAME).glob("*.png"))) == 8
        assert len(calls) == 1, f"the reference was aligned {len(calls)} times"

    def test_the_reference_is_loaded_exactly_once(
        self, tmp_path: Path, template, blank_scan: Path, monkeypatch
    ):
        """The same invariant at the generator's own boundary.

        Asserted as well as the alignment count because they can come apart: a
        future change that cached the *homography* but re-decoded the scan for
        every sheet would keep one of these at 1 and send the other to N.
        """
        from omr_scanner.evaluation import synthetic_dataset as module

        real = module.load_reference_scan
        calls: list[int] = []

        def counting(*args: Any, **kwargs: Any) -> ReferenceScan:
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, "load_reference_scan", counting)
        generate(tmp_path / "dataset", template, blank_scan, count=8)
        assert len(calls) == 1

    def test_the_template_mode_never_touches_a_reference(
        self, tmp_path: Path, template, monkeypatch
    ):
        from omr_scanner.evaluation import synthetic_dataset as module

        calls: list[int] = []

        def refuse(*_args: Any, **_kwargs: Any) -> ReferenceScan:
            calls.append(1)
            raise AssertionError("the template mode must not register a reference")

        monkeypatch.setattr(module, "load_reference_scan", refuse)
        generate(tmp_path / "dataset", template, None, count=4)
        assert calls == []

    @pytest.mark.parametrize("blank", [True, False])
    def test_no_rendered_image_outlives_the_sheet_that_carried_it(
        self, tmp_path: Path, template, blank_scan: Path, monkeypatch, blank
    ):
        """Bounded memory, stated as an invariant instead of measured as bytes.

        A resident-set measurement would be fragile, machine-dependent and
        would fail for reasons unrelated to this code. The property that
        actually matters is narrower and exactly checkable: the generator must
        not *retain* what it has rendered. One sheet is drawn, encoded, written
        and released before the next begins, so once a run has returned, every
        image it produced must be unreachable.

        Run in both modes, because the reference mode is the one that added new
        arrays to the loop.
        """
        import gc
        import weakref

        from omr_scanner.evaluation import synthetic_dataset as module

        real = module.render_case
        rendered: list[weakref.ReferenceType[Any]] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            sheet = real(*args, **kwargs)
            rendered.append(weakref.ref(sheet.image))
            return sheet

        monkeypatch.setattr(module, "render_case", spy)
        manifest = generate(
            tmp_path / "dataset",
            template,
            blank_scan if blank else None,
            count=6,
        )

        gc.collect()
        # Six candidate sheets and one solution sheet per set, every one of
        # them drawn through the same renderer and released the same way.
        solutions = len(manifest.generator["solutions"]["sets"])
        assert solutions >= 1
        assert len(rendered) == 6 + solutions
        survivors = [index for index, ref in enumerate(rendered) if ref() is not None]
        assert survivors == [], f"sheet image(s) {survivors} were still reachable"


def _load(path: Path, template) -> ReferenceScan:
    """Register a reference scan, for the single-sheet tests."""
    return load_reference_scan(path, template)
