"""Tests for the synthetic dataset generator.

What has to be true of generated test data, in order of importance:

1. **The labels are right.** A dataset whose ground truth does not match what
   was drawn is worse than no dataset: every benchmark run after that is
   measuring the generator's mistakes and attributing them to the engine.
2. **It is reproducible.** A seed must give the same dataset, or a regression
   cannot be told apart from a reshuffle.
3. **The interesting cases are present.** A profile that promises blank
   identifiers and marker damage must contain them by construction, not by
   luck of the draw.
4. **Nothing is hard-coded.** Six digits, four options and one set-code column
   are properties of *a* template, and a generator that assumes them is useless
   for the next one.

The first of those is checked the only way it can be: by *recognising* the
generated sheets and confirming the engine agrees with the labels on the clean
ones, where there is no honest room for disagreement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.benchmark import evaluate
from omr_scanner.evaluation.case_plans import plan_dataset
from omr_scanner.evaluation.ground_truth import (
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GENERATOR_VERSION,
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_CSV_FILENAME,
    MANIFEST_FILENAME,
    SUMMARY_FILENAME,
    CaseFamily,
    DatasetProfile,
    ImageFormat,
    MarkPlan,
    TestCaseTag,
    describe_template,
    generate_dataset,
    page_render_size,
    render_case,
    sheet_spec_from_template,
    validate_template,
)
from omr_scanner.evaluation.test_cases import FieldLayout
from omr_scanner.imaging.synthetic import MarkStyle, render_sheet
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def engine():
    return RecognitionEngine(RecognitionOptions(with_preview=False))


def first_case(template, **kwargs: object):
    """Return the first planned case of a profile, for a single-sheet test."""
    return plan_dataset(template, count=1, seed=1, **kwargs)[0]


class TestSheetsComeFromTheTemplate:
    def test_the_page_matches_the_templates_geometry(self, template):
        spec = sheet_spec_from_template(template)
        assert spec.width == template.page.canonical_width_px
        assert spec.height == template.page.canonical_height_px
        assert set(spec.marker_targets) == {
            marker.role for marker in template.registration_markers
        }

    def test_every_declared_bubble_is_drawn(self, template):
        spec = sheet_spec_from_template(template)
        expected = sum(
            zone.field.rows * zone.field.columns
            for zone in template.zones
            if zone.grid is not None
        )
        assert len(spec.answer_bubbles) == expected

    def test_a_marked_group_is_the_only_one_inked(self, template):
        spec = sheet_spec_from_template(
            template, {"questions_0": {0: MarkPlan(labels=("B",))}}
        )
        inked = [bubble for bubble in spec.answer_bubbles if bubble.fill > 0.0]
        assert len(inked) == 1

    def test_a_structural_case_can_omit_a_marker(self, template):
        spec = sheet_spec_from_template(template, omit_markers=("top_right",))
        assert len(spec.omit_markers) == 1

    def test_marker_damage_reaches_the_renderer(self, template):
        spec = sheet_spec_from_template(
            template,
            faint_markers=("top_left",),
            damaged_markers=("bottom_right",),
            faint_orientation=True,
            extra_marker=True,
        )
        assert len(spec.faint_markers) == 1
        assert len(spec.damaged_markers) == 1
        assert spec.faint_orientation_marker is True
        assert spec.decoy_markers

    def test_mark_styles_reach_the_renderer(self, template):
        spec = sheet_spec_from_template(
            template,
            {"questions_0": {0: MarkPlan(labels=("B",), style=MarkStyle.TICK, offset_x=0.3)}},
        )
        marked = next(bubble for bubble in spec.answer_bubbles if bubble.fill > 0.0)
        assert marked.style is MarkStyle.TICK
        assert marked.offset_x == pytest.approx(0.3)

    def test_every_mark_style_renders_without_error(self, template):
        # The shapes are approximate by design; what must never happen is a
        # style that throws, because one bad style would break a whole dataset.
        for style in MarkStyle:
            spec = sheet_spec_from_template(
                template, {"questions_0": {0: MarkPlan(labels=("B",), style=style)}}
            )
            assert render_sheet(spec).image.any()


class TestResolution:
    def test_sheets_are_rendered_from_the_templates_physical_page_size(self, template):
        # A4 at 150 dpi. Derived, never assumed: a Letter template must come out
        # 1275x1650 without anybody editing this module.
        render = page_render_size(template, 150)
        assert (render.width, render.height) == (1240, 1754)
        assert render.derived_from == "physical"

    def test_resolution_scales_with_dpi(self, template):
        low = page_render_size(template, 100)
        high = page_render_size(template, 300)
        assert high.width == pytest.approx(low.width * 3, rel=0.01)

    def test_a_template_without_millimetres_falls_back_to_its_canonical_size(self, template):
        page = template.page.model_copy(update={"width_mm": 0.0, "height_mm": 0.0})
        flat = template.model_copy(update={"page": page})
        render = page_render_size(flat)
        assert render.derived_from == "canonical"
        assert render.width == template.page.canonical_width_px

    def test_dpi_must_be_positive(self, template):
        with pytest.raises(ValueError, match="positive"):
            page_render_size(template, 0)

    def test_the_rendered_image_really_is_that_size(self, template):
        case = first_case(template, profile=DatasetProfile.BASELINE)
        render = page_render_size(template, 150)
        sheet = render_case(template, case, render=render)
        # A distortion adds a margin around the page, so the image is at least
        # the page - what matters is that it is not the canonical size.
        assert sheet.image.shape[0] >= render.height
        assert sheet.image.shape[1] >= render.width


class TestLabelsMatchWhatWasDrawn:
    def test_a_clean_sheet_reads_exactly_as_labelled(self, tmp_path: Path, template, engine):
        import cv2

        case = first_case(template, profile=DatasetProfile.BASELINE)
        sheet = render_case(template, case)
        path = tmp_path / sheet.truth.scan
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        assert result.identifier_value == sheet.truth.roll
        assert result.set_code_value == sheet.truth.set_code
        for number, expected in sheet.truth.answers.items():
            assert result.answer(number).value == expected, f"question {number}"

    def test_a_baseline_dataset_scores_perfectly(self, tmp_path: Path, template, engine):
        # The end-to-end statement of "the labels are right": generate, read,
        # compare. Anything less than perfect on *clean* sheets is a defect in
        # one of the two halves, and this is how it gets noticed early.
        generate_dataset(
            tmp_path, template, count=4, seed=99, profile=DatasetProfile.BASELINE
        )
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        results = [
            engine.process(path, template)
            for path in sorted((tmp_path / IMAGES_DIRNAME).glob("*.png"))
        ]

        summary = evaluate(results, truths, dataset="baseline").summary
        assert summary.scans == 4
        assert summary.failed == 0
        assert summary.question_accuracy == 1.0
        assert summary.roll_accuracy == 1.0
        assert summary.set_accuracy == 1.0
        assert summary.sheet_accuracy == 1.0

    def test_blanks_and_double_marks_are_labelled_with_the_engines_own_convention(
        self, tmp_path: Path, template, engine
    ):
        import cv2

        marks = {
            "roll_number": {
                index: MarkPlan(labels=(digit,)) for index, digit in enumerate("120317")
            },
            "set_code": {0: MarkPlan(labels=("A",))},
            "questions_0": {
                0: MarkPlan(labels=("A", "C")),  # a deliberate double mark
                1: MarkPlan(labels=("B",)),
                # question 3 left out entirely: blank
            },
        }
        spec = sheet_spec_from_template(template, marks)
        path = tmp_path / "convention.png"
        cv2.imwrite(str(path), render_sheet(spec).image)

        result = engine.process(path, template)
        assert result.answer(1).value == "A-C"
        assert result.answer(2).value == "B"
        assert result.answer(3).value == ""

    def test_the_truth_records_the_marks_and_not_only_the_string(self, template):
        # The marks are the fact; the expected string is an interpretation of
        # them. Storing both is what lets the benchmark re-judge an ambiguous
        # column without regenerating the dataset.
        case = first_case(template, profile=DatasetProfile.BASELINE)
        sheet = render_case(template, case)
        assert len(sheet.truth.roll_marks) == 6
        assert all(len(column) == 1 for column in sheet.truth.roll_marks)


class TestReproducibility:
    def test_the_same_seed_gives_the_same_dataset(self, tmp_path: Path, template):
        first = tmp_path / "first"
        second = tmp_path / "second"
        generate_dataset(first, template, count=3, seed=4242, profile=DatasetProfile.MIXED)
        generate_dataset(second, template, count=3, seed=4242, profile=DatasetProfile.MIXED)

        for name in ("SYN_000001.png", "SYN_000002.png", "SYN_000003.png"):
            assert (first / IMAGES_DIRNAME / name).read_bytes() == (
                second / IMAGES_DIRNAME / name
            ).read_bytes()

    def test_a_different_seed_gives_different_sheets(self, tmp_path: Path, template):
        first = tmp_path / "a"
        second = tmp_path / "b"
        generate_dataset(first, template, count=2, seed=1, profile=DatasetProfile.RECOGNITION)
        generate_dataset(second, template, count=2, seed=2, profile=DatasetProfile.RECOGNITION)
        assert (first / IMAGES_DIRNAME / "SYN_000001.png").read_bytes() != (
            second / IMAGES_DIRNAME / "SYN_000001.png"
        ).read_bytes()

    def test_one_sheet_can_be_rendered_on_its_own(self, template):
        # A plan is data, so reproducing sheet 7 means planning the dataset and
        # rendering one case - not regenerating the six before it.
        case = plan_dataset(template, count=8, seed=500)[6]
        first = render_case(template, case)
        again = render_case(template, case)
        assert first.truth == again.truth
        assert (first.image == again.image).all()

    def test_the_plan_is_a_function_of_the_seed(self, template):
        first = plan_dataset(template, count=12, seed=7)
        again = plan_dataset(template, count=12, seed=7)
        assert [case.tag_values for case in first] == [case.tag_values for case in again]
        assert [case.roll for case in first] == [case.roll for case in again]


class TestTheDatasetOnDisk:
    @pytest.fixture
    def dataset(self, tmp_path: Path, template) -> Path:
        generate_dataset(
            tmp_path, template, count=8, seed=31337, profile=DatasetProfile.MIXED,
            name="unit-dataset", version="3", template_path="sheet.omrt",
        )
        return tmp_path

    def test_it_has_the_documented_layout(self, dataset: Path):
        assert (dataset / IMAGES_DIRNAME).is_dir()
        assert (dataset / GROUND_TRUTH_DIRNAME).is_dir()
        assert (dataset / MANIFEST_FILENAME).is_file()
        assert (dataset / MANIFEST_CSV_FILENAME).is_file()
        assert (dataset / SUMMARY_FILENAME).is_file()

    def test_every_image_has_ground_truth_beside_it(self, dataset: Path):
        images = sorted(path.stem for path in (dataset / IMAGES_DIRNAME).glob("*.png"))
        truths = sorted(path.stem for path in (dataset / GROUND_TRUTH_DIRNAME).glob("*.json"))
        assert images == truths

    def test_the_manifest_records_how_to_regenerate_it(self, dataset: Path):
        manifest = load_manifest(dataset / MANIFEST_FILENAME)
        assert manifest.name == "unit-dataset"
        assert manifest.version == "3"
        assert manifest.generator["seed"] == 31337
        assert manifest.generator["profile"] == "mixed"
        assert manifest.generator["count"] == 8
        assert manifest.generator["generator_version"] == GENERATOR_VERSION
        assert manifest.generator["dpi"] == 150
        assert manifest.generator["image_format"] == "png"
        assert len(manifest.entries) == 8

    def test_the_csv_manifest_lists_every_sheet_with_its_tags(self, dataset: Path):
        import csv

        with (dataset / MANIFEST_CSV_FILENAME).open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 8
        assert all(row["image"] and row["ground_truth"] for row in rows)
        assert any(row["tags"] for row in rows)

    def test_the_summary_counts_the_kinds_of_sheet_it_contains(self, dataset: Path):
        import json

        payload = json.loads((dataset / SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert payload["image_count"] == 8
        assert payload["tag_counts"]
        assert sum(payload["tag_counts"].values()) >= 8

    def test_the_manifest_says_synthetic_data_is_not_accuracy_evidence(self, dataset: Path):
        # Written into the data itself, because a manifest outlives the
        # conversation in which somebody explained the caveat.
        assert "not evidence" in load_manifest(dataset / MANIFEST_FILENAME).notes

    def test_ground_truth_records_what_was_done_to_each_sheet(self, dataset: Path):
        truths = load_ground_truth_directory(dataset / GROUND_TRUTH_DIRNAME)
        for truth in truths.values():
            assert truth.tags
            assert truth.metadata["seed"] == 31337
            assert truth.metadata["render"]["dpi"] == 150
            assert truth.human_verified is False  # generated, never verified

    def test_roll_numbers_are_fictional_and_fit_the_template(self, dataset: Path):
        truths = load_ground_truth_directory(dataset / GROUND_TRUTH_DIRNAME)
        for truth in truths.values():
            assert len(truth.roll) == 6
            # "_" for a deliberately blank column, "?" for an unreadable one.
            assert all(character in "0123456789_?" for character in truth.roll)

    def test_jpeg_output_is_written_as_jpeg(self, tmp_path: Path, template):
        generate_dataset(
            tmp_path, template, count=2, seed=5, image_format=ImageFormat.JPEG
        )
        images = sorted((tmp_path / IMAGES_DIRNAME).iterdir())
        assert [path.suffix for path in images] == [".jpg", ".jpg"]
        assert images[0].read_bytes()[:2] == b"\xff\xd8"  # JPEG start-of-image

    def test_a_dataset_needs_at_least_one_sheet(self, tmp_path: Path, template):
        with pytest.raises(ValueError, match="at least one sheet"):
            generate_dataset(tmp_path, template, count=0)

    def test_a_template_with_no_bubbles_is_refused_with_a_reason(self, tmp_path: Path, template):
        empty = template.model_copy(update={"zones": ()})
        with pytest.raises(ValueError, match="no bubble grids"):
            generate_dataset(tmp_path, empty, count=1)

    def test_an_unusable_template_is_reported_before_anything_is_written(self, template):
        empty = template.model_copy(update={"zones": ()})
        assert "no bubble grids" in " ".join(validate_template(empty))
        assert validate_template(template) == ()


class TestProgressAndCancellation:
    def test_progress_is_reported_once_per_sheet(self, tmp_path: Path, template):
        seen: list[tuple[int, int]] = []
        generate_dataset(
            tmp_path,
            template,
            count=5,
            seed=3,
            on_progress=lambda progress: seen.append((progress.completed, progress.total)),
        )
        assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]

    def test_cancelling_stops_early_and_still_writes_a_usable_dataset(
        self, tmp_path: Path, template
    ):
        # A cancelled run is not a failed one: what was written is a real
        # dataset of fewer sheets, and its manifest says so rather than
        # promising sheets that are not there.
        state = {"count": 0}

        def cancel() -> bool:
            state["count"] += 1
            return state["count"] > 3

        manifest = generate_dataset(
            tmp_path, template, count=20, seed=3, should_cancel=cancel
        )
        assert len(manifest.entries) == 3
        assert manifest.generator["cancelled"] is True
        assert manifest.generator["requested_count"] == 20
        assert len(list((tmp_path / IMAGES_DIRNAME).iterdir())) == 3


class TestTheCasesAreActuallyThere:
    def test_the_baseline_profile_contains_no_deliberate_failures(
        self, tmp_path: Path, template
    ):
        generate_dataset(tmp_path, template, count=4, seed=5, profile=DatasetProfile.BASELINE)
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        assert all(not truth.expect_failure for truth in truths.values())

    def test_the_recognition_profile_covers_the_mark_cases(self, template):
        cases = plan_dataset(
            template, count=200, seed=11, profile=DatasetProfile.RECOGNITION
        )
        tags = {tag for case in cases for tag in case.tags}
        for required in (
            TestCaseTag.BLANK_STUDENT_ID,
            TestCaseTag.MULTIPLE_STUDENT_ID,
            TestCaseTag.BLANK_QUESTION,
            TestCaseTag.MULTIPLE_ANSWER,
            TestCaseTag.FAINT_MARK,
            TestCaseTag.ERASED_MARK,
            TestCaseTag.INTENSITY_SWEEP,
        ):
            assert required in tags, required

    def test_the_degradation_profile_covers_the_scanner_cases(self, template):
        cases = plan_dataset(
            template, count=200, seed=11, profile=DatasetProfile.DEGRADATION
        )
        tags = {tag for case in cases for tag in case.tags}
        for required in (
            TestCaseTag.ROTATION_MILD,
            TestCaseTag.PERSPECTIVE_MODERATE,
            TestCaseTag.CROP_MILD,
            TestCaseTag.MARKER_DAMAGED,
            TestCaseTag.BLUR,
            TestCaseTag.NOISE,
        ):
            assert required in tags, required

    def test_the_edge_cases_survive_a_dataset_too_small_to_hold_them_all(self, template):
        # The whole point of planning mandatory cases first: twelve sheets of a
        # mixed dataset must still be twelve *different kinds* of sheet, not
        # twelve baselines.
        cases = plan_dataset(template, count=12, seed=3, profile=DatasetProfile.MIXED)
        families = {case.tags[0] for case in cases}
        assert len(families) >= 8

    def test_a_custom_profile_uses_exactly_the_families_it_was_given(self, template):
        cases = plan_dataset(
            template,
            count=30,
            seed=4,
            profile=DatasetProfile.CUSTOM,
            custom_families=[CaseFamily.GEOMETRY],
        )
        tags = {tag for case in cases for tag in case.tags}
        assert TestCaseTag.ROTATION_MILD in tags
        assert TestCaseTag.BLUR not in tags

    def test_duplicate_identifier_groups_are_planted_and_labelled(self, template):
        cases = plan_dataset(
            template,
            count=40,
            seed=6,
            profile=DatasetProfile.CUSTOM,
            custom_families=[CaseFamily.DUPLICATES],
        )
        groups: dict[str, set[tuple[tuple[str, ...], ...]]] = {}
        for case in cases:
            if case.duplicate_group:
                groups.setdefault(case.duplicate_group, set()).add(case.roll_marks)
        assert groups
        # Every sheet in a group has the *same digits drawn on it*: the engine
        # is right to read the number twice, and the duplicate is a batch-level
        # fact. Compared on the marks rather than on the expected string,
        # because one member of the low-confidence pair is drawn faintly and is
        # therefore expected to read as unresolved.
        assert all(len(drawn) == 1 for drawn in groups.values())

    def test_borderline_marks_are_labelled_as_ambiguous(self, template):
        cases = plan_dataset(
            template, count=60, seed=12, profile=DatasetProfile.RECOGNITION
        )
        assert any(case.ambiguous_questions for case in cases)

    def test_a_sheet_expected_to_fail_really_does(self, tmp_path: Path, template, engine):
        import cv2

        from omr_scanner.services.recognition_models import RegistrationStatus

        cases = plan_dataset(
            template,
            count=30,
            seed=3,
            profile=DatasetProfile.CUSTOM,
            custom_families=[CaseFamily.MARKERS],
        )
        case = next(item for item in cases if item.expect_failure)
        sheet = render_case(template, case)
        path = tmp_path / sheet.truth.scan
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        assert result.registration is RegistrationStatus.FAILED

    def test_mark_styles_are_varied_across_a_dataset(self, template):
        cases = plan_dataset(
            template,
            count=20,
            seed=8,
            profile=DatasetProfile.CUSTOM,
            custom_families=[CaseFamily.MARK_STYLES],
        )
        styles = {
            plan.style
            for case in cases
            for zone in case.marks.values()
            for plan in zone.values()
        }
        assert len(styles) > 2


class TestNothingIsHardCoded:
    """A generator that assumes six digits and four options is single-use.

    These are the tests that make the "one template definition" rule real: the
    same code has to produce a sensible dataset for a template it has never
    seen, with a different identifier length, different options and no set code
    at all.
    """

    def test_a_template_with_a_different_shape_is_read_from_the_template(self):
        template = build_answer_sheet_template(
            roll_digits=9,
            answer_labels=("A", "B", "C", "D", "E"),
            question_blocks=1,
            questions_per_block=15,
        )
        layout = FieldLayout.of(template)
        assert layout.identifier_columns == 9
        assert layout.option_labels == ("A", "B", "C", "D", "E")
        assert len(layout.questions) == 15

    def test_identifiers_are_generated_to_the_templates_own_length(self, tmp_path: Path):
        template = build_answer_sheet_template(roll_digits=9)
        generate_dataset(
            tmp_path, template, count=3, seed=2, profile=DatasetProfile.BASELINE
        )
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        assert all(len(truth.roll) == 9 for truth in truths.values())

    def test_five_option_questions_use_all_five(self):
        template = build_answer_sheet_template(answer_labels=("A", "B", "C", "D", "E"))
        cases = plan_dataset(template, count=60, seed=2, profile=DatasetProfile.RECOGNITION)
        used = {
            label
            for case in cases
            for zone in case.marks.values()
            for plan in zone.values()
            for label in plan.labels
        }
        assert "E" in used

    def test_a_template_without_a_set_code_simply_has_no_set_code_cases(self, tmp_path: Path):
        # Skipped, not crashed: an optional field that is absent is a valid
        # template, and the dataset should be the rest of the cases.
        template = build_answer_sheet_template()
        without = template.model_copy(
            update={"zones": tuple(zone for zone in template.zones if zone.id != "set_code")}
        )
        generate_dataset(tmp_path, without, count=6, seed=2, profile=DatasetProfile.MIXED)
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        assert truths
        assert all(truth.set_code == "" for truth in truths.values())

    def test_describe_reports_what_a_template_offers(self):
        template = build_answer_sheet_template(roll_digits=8, questions_per_block=12)
        described = describe_template(template)
        assert described["identifier_columns"] == 8
        assert described["questions"] == 24  # two blocks of twelve
        assert described["dpi"] == 150
