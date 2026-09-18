"""Tests for the synthetic dataset generator.

What has to be true of generated test data, in order of importance:

1. **The labels are right.** A dataset whose ground truth does not match what
   was drawn is worse than no dataset: every benchmark run after that is
   measuring the generator's mistakes and attributing them to the engine.
2. **It is reproducible.** A seed must give the same dataset, or a regression
   cannot be told apart from a reshuffle.
3. **It is varied.** Marks, geometry, exposure and structural damage, in the
   proportions each profile promises.

The first of those is checked the only way it can be: by *recognising* the
generated sheets and confirming the engine agrees with the labels on the clean
ones, where there is no honest room for disagreement.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.benchmark import evaluate
from omr_scanner.evaluation.ground_truth import (
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GENERATOR_VERSION,
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
    DatasetProfile,
    MarkPlan,
    build_sheet,
    generate_dataset,
    sheet_spec_from_template,
)
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


class TestLabelsMatchWhatWasDrawn:
    def test_a_clean_sheet_reads_exactly_as_labelled(self, tmp_path: Path, template, engine):
        import cv2

        sheet = build_sheet(template, index=1, kind="clean", seed=11)
        path = tmp_path / sheet.truth.scan
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        assert result.identifier_value == sheet.truth.roll
        assert result.set_code_value == sheet.truth.set_code
        for number, expected in sheet.truth.answers.items():
            assert result.answer(number).value == expected, f"question {number}"

    def test_a_clean_dataset_scores_perfectly(self, tmp_path: Path, template, engine):
        # The end-to-end statement of "the labels are right": generate, read,
        # compare. Anything less than perfect on *clean* sheets is a defect in
        # one of the two halves, and this is how it gets noticed early.
        generate_dataset(
            tmp_path, template, count=4, seed=99, profile=DatasetProfile.CLEAN
        )
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        results = [
            engine.process(path, template)
            for path in sorted((tmp_path / IMAGES_DIRNAME).glob("*.png"))
        ]

        summary = evaluate(results, truths, dataset="clean").summary
        assert summary.scans == 4
        assert summary.failed == 0
        assert summary.question_accuracy == 1.0
        assert summary.roll_accuracy == 1.0
        assert summary.set_accuracy == 1.0

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
        generate_dataset(first, template, count=2, seed=1, profile=DatasetProfile.NORMAL)
        generate_dataset(second, template, count=2, seed=2, profile=DatasetProfile.NORMAL)
        assert (first / IMAGES_DIRNAME / "SYN_000001.png").read_bytes() != (
            second / IMAGES_DIRNAME / "SYN_000001.png"
        ).read_bytes()

    def test_one_sheet_can_be_regenerated_on_its_own(self, template):
        # Each sheet derives its own seed, so reproducing sheet 7 does not mean
        # regenerating the six before it.
        first = build_sheet(template, index=7, kind="normal", seed=500)
        again = build_sheet(template, index=7, kind="normal", seed=500)
        assert first.truth == again.truth
        assert (first.image == again.image).all()

    def test_growing_a_dataset_does_not_reshuffle_it(self, tmp_path: Path, template):
        small = tmp_path / "small"
        large = tmp_path / "large"
        generate_dataset(small, template, count=2, seed=77, profile=DatasetProfile.CLEAN)
        generate_dataset(large, template, count=4, seed=77, profile=DatasetProfile.CLEAN)
        assert (small / IMAGES_DIRNAME / "SYN_000001.png").read_bytes() == (
            large / IMAGES_DIRNAME / "SYN_000001.png"
        ).read_bytes()


class TestTheDatasetOnDisk:
    @pytest.fixture
    def dataset(self, tmp_path: Path, template) -> Path:
        generate_dataset(
            tmp_path, template, count=6, seed=31337, profile=DatasetProfile.MIXED,
            name="unit-dataset", version="3", template_path="sheet.omrt",
        )
        return tmp_path

    def test_it_has_the_documented_layout(self, dataset: Path):
        assert (dataset / IMAGES_DIRNAME).is_dir()
        assert (dataset / GROUND_TRUTH_DIRNAME).is_dir()
        assert (dataset / MANIFEST_FILENAME).is_file()

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
        assert manifest.generator["count"] == 6
        assert manifest.generator["generator_version"] == GENERATOR_VERSION
        assert len(manifest.entries) == 6

    def test_the_manifest_says_synthetic_data_is_not_accuracy_evidence(self, dataset: Path):
        # Written into the data itself, because a manifest outlives the
        # conversation in which somebody explained the caveat.
        assert "not evidence" in load_manifest(dataset / MANIFEST_FILENAME).notes

    def test_ground_truth_records_the_defects_that_were_injected(self, dataset: Path):
        truths = load_ground_truth_directory(dataset / GROUND_TRUTH_DIRNAME)
        for truth in truths.values():
            assert truth.metadata["seed"]
            assert truth.metadata["case_kind"] in {"clean", "normal", "difficult", "broken"}
            assert "distortion" in truth.metadata
            assert truth.human_verified is False  # generated, never verified

    def test_roll_numbers_are_fictional_and_fit_the_template(self, dataset: Path):
        truths = load_ground_truth_directory(dataset / GROUND_TRUTH_DIRNAME)
        for truth in truths.values():
            assert truth.roll.isdigit()
            assert len(truth.roll) == 6

    def test_a_dataset_needs_at_least_one_sheet(self, tmp_path: Path, template):
        with pytest.raises(ValueError, match="at least one sheet"):
            generate_dataset(tmp_path, template, count=0)

    def test_a_template_with_no_bubbles_is_refused_with_a_reason(self, tmp_path: Path, template):
        empty = template.model_copy(update={"zones": ()})
        with pytest.raises(ValueError, match="no bubble grids"):
            generate_dataset(tmp_path, empty, count=1)


class TestProfiles:
    @pytest.mark.parametrize(
        "profile", [DatasetProfile.CLEAN, DatasetProfile.NORMAL, DatasetProfile.DIFFICULT]
    )
    def test_every_ordinary_profile_produces_readable_sheets(
        self, tmp_path: Path, template, profile
    ):
        generate_dataset(tmp_path, template, count=2, seed=5, profile=profile)
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        assert all(not truth.expect_failure for truth in truths.values())

    def test_the_stress_profile_includes_structural_damage(self, tmp_path: Path, template):
        generate_dataset(tmp_path, template, count=6, seed=6, profile=DatasetProfile.STRESS)
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        broken = [truth for truth in truths.values() if truth.expect_failure]
        assert broken
        # Rotated through rather than rolled for, so a small stress dataset is
        # guaranteed to contain more than one kind of damage.
        kinds = {tuple(sorted(truth.metadata["defects"])) for truth in broken}
        assert len(kinds) > 1

    def test_a_sheet_expected_to_fail_really_does(self, tmp_path: Path, template, engine):
        import cv2

        from omr_scanner.services.recognition_models import RegistrationStatus

        sheet = build_sheet(template, index=1, kind="broken", seed=3)
        assert sheet.truth.expect_failure
        path = tmp_path / sheet.truth.scan
        cv2.imwrite(str(path), sheet.image)

        result = engine.process(path, template)
        assert result.registration is RegistrationStatus.FAILED

    def test_the_difficult_profile_varies_the_mark_styles(self, tmp_path: Path, template):
        generate_dataset(tmp_path, template, count=4, seed=8, profile=DatasetProfile.DIFFICULT)
        styles: set[str] = set()
        for path in (tmp_path / GROUND_TRUTH_DIRNAME).glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            styles.update(payload["metadata"]["mark_styles"].values())
        assert len(styles) > 1

    def test_borderline_marks_are_labelled_as_ambiguous(self, tmp_path: Path, template):
        # Drawn on purpose, and labelled so the benchmark scores *flagging*
        # them as correct rather than punishing the engine for its caution.
        generate_dataset(tmp_path, template, count=6, seed=12, profile=DatasetProfile.DIFFICULT)
        truths = load_ground_truth_directory(tmp_path / GROUND_TRUTH_DIRNAME)
        assert any(truth.ambiguous for truth in truths.values())
