"""A mapped set through the synthetic generator and the real engine (phase 0.1.1-A).

The generator is told that logical Set 10 is printed as ``A`` and Set 11 as
``B`` (``10=A, 11=B``) on a template whose set field offers ``A``-``D``. Its
sheets must carry the **physical** mark - so recognition, which is unchanged,
reads ``A`` - while the ground truth records the logical set. The solution
sheets follow the same rule, and the Answer Key stage's check files them
under the logical set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.exam_sets import ExamSet
from omr_scanner.domain.set_identity import SetIdentity
from omr_scanner.evaluation.answer_keys import SOLUTION_DIRNAME
from omr_scanner.evaluation.attendance_dataset import parse_set_spec, plan_population
from omr_scanner.evaluation.ground_truth import load_ground_truth, load_ground_truth_directory
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    DatasetProfile,
    generate_dataset,
)
from omr_scanner.services.answer_key import SetCodeCheck, check_sheet_set, key_from_scan, plan_for
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions

MARKS = {"10": "A", "11": "B"}
IDENTITY = SetIdentity(
    [ExamSet(set_id="ten", code="10", physical_mark="A"),
     ExamSet(set_id="eleven", code="11", physical_mark="B")]
)


@pytest.fixture(scope="module")
def engine() -> RecognitionEngine:
    return RecognitionEngine(RecognitionOptions(with_preview=False))


@pytest.fixture(scope="module")
def mapped(tmp_path_factory: pytest.TempPathFactory):
    template = build_answer_sheet_template()
    population = plan_population(count=12, set_codes=("10", "11"), seed=7)
    out = tmp_path_factory.mktemp("mapped")
    manifest = generate_dataset(
        out,
        template,
        seed=7,
        profile=DatasetProfile.BASELINE,
        population=population,
        write_metadata=False,
        physical_marks=MARKS,
    )
    return out, manifest, template


class TestTheSetSpecification:
    def test_codes_and_marks(self) -> None:
        assert parse_set_spec("10=A, 11=B, 12") == (("10", "11", "12"), {"10": "A", "11": "B"})

    def test_plain_codes_are_unchanged(self) -> None:
        assert parse_set_spec("10, 11, 10") == (("10", "11"), {})

    @pytest.mark.parametrize("bad", ["=A", "10=", " = "])
    def test_a_half_pair_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_set_spec(bad)


class TestSolutionSheets:
    def test_the_manifest_records_the_mapping(self, mapped) -> None:
        _out, manifest, _template = mapped
        assert manifest.generator["physical_marks"] == MARKS
        entries = {entry["set_code"]: entry for entry in manifest.generator["solutions"]["sets"]}
        assert entries["10"]["physical_mark"] == "A"
        assert entries["11"]["physical_mark"] == "B"
        assert all(entry["set_code_marked"] for entry in entries.values())

    def test_the_ground_truth_keeps_the_physical_mark_and_the_logical_set(self, mapped) -> None:
        out, _manifest, _template = mapped
        truth = load_ground_truth(out / SOLUTION_DIRNAME / "Set_10_Solution.json")
        assert truth.set_code == "A"
        assert truth.metadata["logical_set"] == "10"

    def test_the_engine_reads_the_mark_and_the_check_files_it_under_set_10(
        self, mapped, engine: RecognitionEngine
    ) -> None:
        out, _manifest, template = mapped
        plan = plan_for(template)
        result = engine.process(out / SOLUTION_DIRNAME / "Set_10_Solution.png", template)
        scanned = key_from_scan(result, plan)
        assert scanned.set_code == "A"
        verdict = check_sheet_set(
            template, selected="10", read=scanned.set_code, identity=IDENTITY
        )
        assert verdict.check is SetCodeCheck.MATCH, verdict.message
        assert verdict.read_set == "10"
        wrong = check_sheet_set(template, selected="11", read=scanned.set_code, identity=IDENTITY)
        assert wrong.check is SetCodeCheck.MISMATCH
        assert wrong.sheet_set == "10"
        assert "10 (A on sheet)" in wrong.message


class TestCandidateSheets:
    def test_every_mapped_sheet_carries_the_mark_and_records_its_set(self, mapped) -> None:
        out, _manifest, _template = mapped
        truths = load_ground_truth_directory(out / GROUND_TRUTH_DIRNAME)
        logical = [truth for truth in truths.values() if "logical_set" in truth.metadata]
        assert logical, "no candidate sheet recorded a logical set"
        for truth in logical:
            assert MARKS[truth.metadata["logical_set"]] == truth.set_code

    def test_a_clean_sheet_reads_as_its_mark_and_translates_to_its_set(
        self, mapped, engine: RecognitionEngine
    ) -> None:
        out, _manifest, template = mapped
        truths = load_ground_truth_directory(out / GROUND_TRUTH_DIRNAME)
        checked = 0
        for name, truth in sorted(truths.items()):
            if "logical_set" not in truth.metadata or truth.set_ambiguous:
                continue
            (image,) = sorted((out / IMAGES_DIRNAME).glob(f"{Path(name).stem}.*"))
            result = engine.process(image, template)
            assert result.set_code_value == truth.set_code
            assert IDENTITY.logical_for_physical(result.set_code_value) == (
                truth.metadata["logical_set"]
            )
            checked += 1
            if checked == 3:
                break
        assert checked, "no clean mapped sheet to read"
