"""End-to-end tests of the ``solution/`` folder and key-relative candidate answers.

What has to be true, in order of importance:

1. **OMRFlow can read its own solution sheets.** The recognition engine reads
   every solution sheet back as its set code and its key - checked by running
   the engine, not by inspecting pixels.
2. **One key, three statements.** The solution sheet, the text file and the
   manifest say exactly the same thing for every set.
3. **Candidates answer their own set's key.** Every candidate's intended
   answers agree with the key of the paper they sat in exactly the drawn number
   of questions, and scoring them through OMRFlow's scorer gives that number.
4. **Nothing else moved.** Switching solutions off changes which files exist
   and nothing about any candidate's sheet; the same seed reproduces the same
   bytes.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import pytest
from tests.conftest import REPOSITORY_ROOT, build_answer_sheet_template

from omr_scanner.domain.scoring import (
    AnswerKeyStatus,
    ScoringPolicy,
    canonical_answer_string,
    score_answers,
)
from omr_scanner.evaluation.answer_keys import SOLUTION_DIRNAME, SOLUTION_ROLE
from omr_scanner.evaluation.attendance_dataset import bind_case, plan_population
from omr_scanner.evaluation.case_plans import plan_dataset
from omr_scanner.evaluation.ground_truth import (
    DatasetManifest,
    load_ground_truth,
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.performance import LEGACY_RANDOM, PerformancePolicy
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
    ColorMode,
    DatasetProfile,
    ImageFormat,
    generate_dataset,
)
from omr_scanner.evaluation.test_cases import FieldLayout, TestCaseTag
from omr_scanner.services.answer_key import key_from_scan, plan_for, read_key
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions
from omr_scanner.services.template_service import load_template
from omr_scanner.tools import make_dataset

if TYPE_CHECKING:
    from omr_scanner.domain.scoring import AnswerKey
    from omr_scanner.domain.template import OmrTemplate

ECE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
SETS = ("10", "11", "12")
SEED = 20260929


@pytest.fixture(scope="module")
def engine():
    return RecognitionEngine(RecognitionOptions(with_preview=False))


@pytest.fixture(scope="module")
def ece():
    return load_template(ECE_TEMPLATE)


@pytest.fixture(scope="module")
def exam(tmp_path_factory, ece):
    """Three sets, 100 questions, 120 registered candidates."""
    population = plan_population(count=120, set_codes=SETS, seed=SEED)
    out = tmp_path_factory.mktemp("exam")
    manifest = generate_dataset(
        out,
        ece,
        seed=SEED,
        profile=DatasetProfile.MIXED,
        population=population,
    )
    return out, manifest, population


def key_text(out: Path, code: str) -> str:
    return (out / SOLUTION_DIRNAME / f"Set_{code}_Answer_Key.txt").read_text(encoding="utf-8")


def candidate_truths(out: Path, manifest):
    return [load_ground_truth(out / GROUND_TRUTH_DIRNAME / name) for name in manifest.entries]


def small(tmp_path: Path, name: str, **kwargs: Any) -> tuple[Path, DatasetManifest]:
    """A small paired dataset on the synthetic test template."""
    template = kwargs.pop("template", None) or build_answer_sheet_template()
    sets = kwargs.pop("sets", ("A", "B", "C"))
    population = plan_population(count=kwargs.pop("count", 14), set_codes=sets, seed=31)
    out = tmp_path / name
    manifest = generate_dataset(
        out,
        template,
        seed=31,
        profile=kwargs.pop("profile", DatasetProfile.MIXED),
        population=population,
        write_metadata=False,
        **kwargs,
    )
    return out, manifest


class TestTheSolutionFolder:
    def test_one_sheet_one_key_and_one_truth_per_set_and_nothing_else(self, exam):
        out, _manifest, _population = exam
        names = sorted(path.name for path in (out / SOLUTION_DIRNAME).iterdir())
        expected = sorted(
            f"Set_{code}{suffix}"
            for code in SETS
            for suffix in ("_Solution.png", "_Answer_Key.txt", "_Solution.json")
        )
        assert names == expected

    def test_the_text_file_the_manifest_and_the_sheet_agree(self, exam, ece):
        out, manifest, _population = exam
        plan = plan_for(ece)
        blocks = {entry["set_code"]: entry for entry in manifest.generator["solutions"]["sets"]}
        keys = {entry["set_code"]: entry for entry in manifest.generator["answer_keys"]}
        for code in SETS:
            text = key_text(out, code)
            draft = read_key(text, plan, code)
            assert draft.is_valid, draft.issues
            assert draft.answers == blocks[code]["answers"] == keys[code]["answers"]
            assert blocks[code]["set_code_marked"] is True
            truth = load_ground_truth(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.json")
            assert truth.metadata["role"] == SOLUTION_ROLE
            assert truth.set_code == code
            assert set(truth.roll) == {"_"}
            drawn = "".join(truth.answers[number].upper() for number in plan.numbers)
            assert drawn == draft.answers

    def test_the_sets_have_different_keys(self, exam):
        out, _manifest, _population = exam
        assert len({key_text(out, code) for code in SETS}) == 3

    def test_the_engine_reads_every_solution_sheet_as_its_key(self, exam, ece, engine):
        """The test that proves OMRFlow can read its own solution sheets."""
        out, _manifest, _population = exam
        plan = plan_for(ece)
        for code in SETS:
            result = engine.process(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.png", ece)
            scanned = key_from_scan(result, plan)
            assert scanned.is_clean, scanned.summary
            assert scanned.set_code == code
            assert scanned.answers == read_key(key_text(out, code), plan, code).answers

    def test_no_solution_sheet_is_a_candidate_script_or_the_other_way_round(self, exam):
        out, manifest, population = exam
        images = {path.name for path in (out / IMAGES_DIRNAME).iterdir()}
        assert not any("Solution" in name for name in images)
        assert not any(name.startswith("Set_") for name in manifest.entries)
        rolls = {candidate.roll for candidate in population.candidates}
        for code in SETS:
            truth = load_ground_truth(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.json")
            assert truth.roll not in rolls
        for truth in candidate_truths(out, manifest):
            assert truth.metadata.get("role") != SOLUTION_ROLE
        with (out / GROUND_TRUTH_DIRNAME / "reconciliation.csv").open(encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                assert "Solution" not in "".join(row.values())


class TestCandidatesAnswerTheirOwnSetsKey:
    def _keys(self, out: Path, ece: OmrTemplate) -> dict[str, AnswerKey]:
        plan = plan_for(ece)
        return {code: read_key(key_text(out, code), plan, code).to_key() for code in SETS}

    def test_every_candidate_answered_the_paper_they_sat(self, exam):
        out, manifest, population = exam
        truths = candidate_truths(out, manifest)
        sheets = population.sheets_to_render()
        assert len(truths) == len(sheets)
        for truth, candidate in zip(truths, sheets, strict=True):
            assert truth.metadata["performance"]["answer_key_set"] == candidate.set_code
            assert candidate.set_code in SETS

    def test_intended_answers_score_exactly_their_target_through_omrflow(self, exam, ece):
        out, manifest, _population = exam
        keys = self._keys(out, ece)
        plan = plan_for(ece)
        policy = ScoringPolicy()
        by_set: dict[str, int] = {}
        for truth in candidate_truths(out, manifest):
            performance = truth.metadata["performance"]
            code = performance["answer_key_set"]
            intended = {int(n): v for n, v in performance["intended_answers"].items()}
            answers = canonical_answer_string(intended, plan.numbers, plan.labels)
            key = replace(keys[code], status=AnswerKeyStatus.VERIFIED)
            breakdown = score_answers(answers, key, policy, first_question=plan.first_question)
            assert breakdown.correct_count == performance["target_correct"]
            by_set[code] = by_set.get(code, 0) + 1
        assert set(by_set) == set(SETS)

    def test_changing_one_sets_key_changes_only_that_sets_marks(self, exam, ece):
        out, manifest, _population = exam
        keys = self._keys(out, ece)
        plan = plan_for(ece)
        policy = ScoringPolicy()
        flipped = "".join("B" if ch == "A" else "A" for ch in keys["11"].answers)
        altered = {**keys, "11": replace(keys["11"], answers=flipped)}

        changed = dict.fromkeys(SETS, 0)
        for truth in candidate_truths(out, manifest):
            performance = truth.metadata["performance"]
            code = performance["answer_key_set"]
            intended = {int(n): v for n, v in performance["intended_answers"].items()}
            answers = canonical_answer_string(intended, plan.numbers, plan.labels)
            before = score_answers(answers, keys[code], policy).correct_count
            after = score_answers(answers, altered[code], policy).correct_count
            changed[code] += before != after
        assert changed["10"] == 0
        assert changed["12"] == 0
        assert changed["11"] > 0

    def test_the_cohort_is_bell_shaped_around_the_configured_mean(self, exam):
        _out, manifest, _population = exam
        observed = manifest.generator["performance"]["observed"]
        assert observed["candidates"] == len(manifest.entries)
        # 110-odd scripts: standard error of the mean is ~1.4 %; four of them.
        assert observed["mean"] == pytest.approx(0.65, abs=0.06)
        assert 0.09 < observed["stddev"] < 0.21
        histogram = list(observed["histogram"].values())
        middle = sum(histogram[5:8])  # 50-80 %
        assert middle > sum(histogram) / 2

    def test_clean_candidate_sheets_are_read_and_scored_to_their_target(self, exam, ece, engine):
        """Recognition and scoring together, on the sheets with no test condition."""
        out, manifest, _population = exam
        keys = self._keys(out, ece)
        plan = plan_for(ece)
        clean = [
            truth
            for truth in candidate_truths(out, manifest)
            if truth.tags == (TestCaseTag.BASELINE.value, TestCaseTag.ALL_ANSWERED.value)
            and not truth.degradation
            and not truth.roll_ambiguous
        ][:4]
        assert clean, "the Mixed profile should contain clean fully-answered sheets"
        for truth in clean:
            result = engine.process(out / IMAGES_DIRNAME / truth.scan, ece)
            recognised = {item.number: item.value for item in result.answers}
            answers = canonical_answer_string(recognised, plan.numbers, plan.labels)
            performance = truth.metadata["performance"]
            key = keys[performance["answer_key_set"]]
            breakdown = score_answers(answers, key, ScoringPolicy())
            assert breakdown.correct_count == performance["target_correct"], truth.scan


class TestReproducibility:
    def test_the_same_seed_writes_byte_identical_solutions(self, tmp_path: Path):
        first, _ = small(tmp_path, "one")
        second, _ = small(tmp_path, "two")
        for path in sorted((first / SOLUTION_DIRNAME).iterdir()):
            assert path.read_bytes() == (second / SOLUTION_DIRNAME / path.name).read_bytes()

    def test_switching_solutions_off_changes_no_candidate_sheet(self, tmp_path: Path):
        on, manifest_on = small(tmp_path, "on")
        off, manifest_off = small(tmp_path, "off", generate_solutions=False)
        assert manifest_on.entries == manifest_off.entries
        for name in manifest_on.entries:
            assert (on / GROUND_TRUTH_DIRNAME / name).read_bytes() == (
                off / GROUND_TRUTH_DIRNAME / name
            ).read_bytes()
        for path in sorted((on / IMAGES_DIRNAME).iterdir()):
            assert path.read_bytes() == (off / IMAGES_DIRNAME / path.name).read_bytes()
        assert manifest_off.generator["solutions"] is None
        assert manifest_off.generator["answer_keys"] == manifest_on.generator["answer_keys"]
        assert not (off / SOLUTION_DIRNAME).exists() or not any(
            (off / SOLUTION_DIRNAME).iterdir()
        )

    def test_the_legacy_mode_reproduces_the_pre_key_answers(self, tmp_path: Path):
        """``random`` is exactly what the generator drew before keys existed."""
        template = build_answer_sheet_template()
        population = plan_population(count=14, set_codes=("A", "B", "C"), seed=31)
        out, manifest = small(tmp_path, "legacy", performance=LEGACY_RANDOM)
        planned = plan_dataset(
            template,
            count=len(population.sheets_to_render()),
            seed=31,
            profile=DatasetProfile.MIXED,
        )
        layout = FieldLayout.of(template)
        bound = [
            bind_case(case, candidate, layout)
            for case, candidate in zip(planned, population.sheets_to_render(), strict=True)
        ]
        for name, case in zip(manifest.entries, bound, strict=True):
            truth = load_ground_truth(out / GROUND_TRUTH_DIRNAME / name)
            assert truth.answers == case.answers
            assert "performance" not in truth.metadata
        assert manifest.generator["performance"]["observed"] is None
        # Keys and solution sheets are still written in the legacy mode.
        assert len(manifest.generator["solutions"]["sets"]) == 3

    def test_performance_settings_move_answers_and_nothing_else(self, tmp_path: Path):
        base, manifest = small(tmp_path, "base")
        high, _ = small(tmp_path, "high", performance=PerformancePolicy(mean=0.9, stddev=0.05))
        for name in manifest.entries:
            a = load_ground_truth(base / GROUND_TRUTH_DIRNAME / name)
            b = load_ground_truth(high / GROUND_TRUTH_DIRNAME / name)
            assert (a.roll, a.set_code, a.tags, a.degradation) == (
                b.roll,
                b.set_code,
                b.tags,
                b.degradation,
            )
        for path in sorted((base / SOLUTION_DIRNAME).iterdir()):
            assert path.read_bytes() == (high / SOLUTION_DIRNAME / path.name).read_bytes()


class TestConfigurations:
    def test_one_set_writes_one_solution(self, tmp_path: Path):
        out, manifest = small(tmp_path, "single", sets=("B",))
        assert sorted(path.name for path in (out / SOLUTION_DIRNAME).iterdir()) == [
            "Set_B_Answer_Key.txt",
            "Set_B_Solution.json",
            "Set_B_Solution.png",
        ]
        assert len(manifest.generator["solutions"]["sets"]) == 1

    def test_jpeg_colour_solutions_follow_the_dataset_format(self, tmp_path: Path, engine):
        template = build_answer_sheet_template()
        out, _ = small(
            tmp_path,
            "jpeg",
            template=template,
            image_format=ImageFormat.JPEG,
            color_mode=ColorMode.COLOR,
        )
        plan = plan_for(template)
        for code in ("A", "B", "C"):
            path = out / SOLUTION_DIRNAME / f"Set_{code}_Solution.jpg"
            image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            assert image is not None and image.ndim == 3
            scanned = key_from_scan(engine.process(path, template), plan)
            assert scanned.set_code == code
            assert scanned.answers == key_text(out, code).strip()

    @pytest.mark.parametrize(
        ("blocks", "per_block", "labels"),
        [(1, 12, ("A", "B", "C")), (3, 20, ("A", "B", "C", "D", "E"))],
    )
    def test_other_question_and_option_counts_are_read_back(
        self, tmp_path: Path, engine, blocks, per_block, labels
    ):
        template = build_answer_sheet_template(
            question_blocks=blocks, questions_per_block=per_block, answer_labels=labels
        )
        out, _ = small(tmp_path, f"shape{blocks}", template=template, count=6)
        plan = plan_for(template)
        for code in ("A", "B", "C"):
            text = key_text(out, code).strip()
            assert len(text) == blocks * per_block
            assert set(text) <= set(labels)
            scanned = key_from_scan(
                engine.process(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.png", template),
                plan,
            )
            assert scanned.answers == text

    def test_multi_character_set_codes_are_marked_and_read(self, tmp_path: Path, engine):
        template = build_answer_sheet_template(set_symbols=("1", "12", "103"))
        out, manifest = small(tmp_path, "symbolic", template=template, sets=("1", "12", "103"))
        plan = plan_for(template)
        for code in ("1", "12", "103"):
            result = engine.process(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.png", template)
            assert key_from_scan(result, plan).set_code == code
        # The candidate sheets carry the same whole-code marks.
        truths = load_ground_truth_directory(out / GROUND_TRUTH_DIRNAME)
        assert {truth.set_code for truth in truths.values()} >= {"1", "12", "103"}
        assert all(entry["set_code_marked"] for entry in manifest.generator["solutions"]["sets"])

    def test_a_set_the_field_cannot_show_leaves_it_blank_and_says_so(self, tmp_path: Path):
        out, manifest = small(tmp_path, "unmarkable", sets=("10", "11"))
        entries = manifest.generator["solutions"]["sets"]
        assert [entry["set_code_marked"] for entry in entries] == [False, False]
        truth = load_ground_truth(out / SOLUTION_DIRNAME / "Set_10_Solution.json")
        assert truth.set_code == "_"

    def test_a_dataset_without_a_roster_keys_the_sets_it_prints(self, tmp_path: Path):
        template = build_answer_sheet_template()
        out = tmp_path / "images_only"
        manifest = generate_dataset(out, template, count=30, seed=4, write_metadata=False)
        printed = {
            truth.set_code
            for truth in load_ground_truth_directory(out / GROUND_TRUTH_DIRNAME).values()
            if len(truth.set_code) == 1 and truth.set_code.isalpha()
        }
        keyed = {entry["set_code"] for entry in manifest.generator["solutions"]["sets"]}
        assert keyed == printed

    def test_a_template_with_no_questions_writes_no_solutions(self, tmp_path: Path):
        template = build_answer_sheet_template(question_blocks=0)
        out = tmp_path / "ids_only"
        manifest = generate_dataset(out, template, count=4, seed=4, write_metadata=False)
        assert manifest.generator["solutions"] is None
        assert manifest.generator["answer_keys"] == []

    def test_stale_solutions_from_an_earlier_run_are_removed(self, tmp_path: Path):
        template = build_answer_sheet_template()
        out = tmp_path / "reused"
        for sets in (("A", "B", "C", "D"), ("A", "B", "C")):
            generate_dataset(
                out,
                template,
                seed=5,
                population=plan_population(count=8, set_codes=sets, seed=5),
                write_metadata=False,
            )
        names = {path.name for path in (out / SOLUTION_DIRNAME).iterdir()}
        assert not any(name.startswith("Set_D_") for name in names)
        assert len(names) == 9

    def test_impossible_performance_settings_are_refused_before_writing(self, tmp_path: Path):
        with pytest.raises(ValueError, match="minimum"):
            small(tmp_path, "bad", performance=PerformancePolicy(minimum=0.8, mean=0.65))
        assert not (tmp_path / "bad").exists()


class TestTheCommandLine:
    def _template(self, tmp_path: Path) -> Path:
        from omr_scanner.services import save_template

        return save_template(build_answer_sheet_template(), tmp_path / "t.omrt")

    def test_solutions_are_written_by_default(self, tmp_path: Path, capsys):
        out = tmp_path / "cli"
        code = make_dataset.main(
            [str(out), "--template", str(self._template(tmp_path)), "--count", "9",
             "--sets", "A,B", "--seed", "3", "--quiet"]
        )
        assert code == 0
        assert (out / SOLUTION_DIRNAME / "Set_A_Answer_Key.txt").is_file()
        assert "2 solution sheet(s)" in capsys.readouterr().out

    def test_no_solutions_and_legacy_answers_can_be_asked_for(self, tmp_path: Path):
        out = tmp_path / "cli"
        code = make_dataset.main(
            [str(out), "--template", str(self._template(tmp_path)), "--count", "9",
             "--sets", "A,B", "--quiet", "--no-solutions",
             "--performance-distribution", "random"]
        )
        assert code == 0
        manifest = load_manifest(out / MANIFEST_FILENAME)
        assert manifest.generator["solutions"] is None
        assert manifest.generator["performance"]["distribution"] == "random"

    def test_the_performance_figures_reach_the_manifest(self, tmp_path: Path):
        out = tmp_path / "cli"
        code = make_dataset.main(
            [str(out), "--template", str(self._template(tmp_path)), "--count", "9",
             "--quiet", "--mean-correct", "0.8", "--sd-correct", "0.1",
             "--min-correct", "0.5", "--max-correct", "0.95"]
        )
        assert code == 0
        performance = load_manifest(out / MANIFEST_FILENAME).generator["performance"]
        assert (performance["mean"], performance["stddev"]) == (0.8, 0.1)
        assert performance["observed"]["minimum"] >= 0.5
        assert performance["observed"]["maximum"] <= 0.95

    def test_impossible_figures_are_refused(self, tmp_path: Path, capsys):
        code = make_dataset.main(
            [str(tmp_path / "cli"), "--template", str(self._template(tmp_path)),
             "--min-correct", "0.9"]
        )
        assert code == make_dataset.EXIT_FAILED
        assert "performance settings" in capsys.readouterr().err


class TestAnUnreadableSetCodeStillSatOnePaper:
    """The internal set behind a deliberately blank or double-marked set field."""

    @staticmethod
    def _key_answers(manifest) -> dict[str, dict[int, str]]:
        return {
            entry["set_code"]: dict(enumerate(entry["answers"], start=entry["first_question"]))
            for entry in manifest.generator["answer_keys"]
        }

    def test_without_a_roster_the_answers_follow_the_assigned_sets_key(
        self, tmp_path: Path
    ):
        from omr_scanner.evaluation.test_cases import CaseFamily

        template = build_answer_sheet_template()
        out = tmp_path / "set_cases"
        manifest = generate_dataset(
            out,
            template,
            count=40,
            seed=8,
            profile=DatasetProfile.CUSTOM,
            custom_families=[CaseFamily.SET_CODE],
            write_metadata=False,
        )
        keys = self._key_answers(manifest)
        unresolved = 0
        for name in manifest.entries:
            truth = load_ground_truth(out / GROUND_TRUTH_DIRNAME / name)
            performance = truth.metadata["performance"]
            assigned = performance["answer_key_set"]
            assert assigned in keys
            printed = truth.set_code
            if "_" in printed or "?" in printed or truth.set_ambiguous:
                unresolved += 1
            else:
                assert assigned == printed
            intended = {int(n): v.upper() for n, v in performance["intended_answers"].items()}
            correct = sum(1 for n, v in intended.items() if keys[assigned][n] == v)
            assert correct == performance["target_correct"], name
        assert unresolved >= 2, "the set-code family should include blank and double marks"

    def test_with_a_roster_the_answers_follow_the_paper_sat_not_the_bubble(
        self, tmp_path: Path
    ):
        from omr_scanner.evaluation.attendance_dataset import ConflictKind

        population = plan_population(count=40, set_codes=("A", "B", "C"), seed=12)
        staged = [
            candidate
            for candidate in population.sheets_to_render()
            if candidate.conflict in {ConflictKind.WRONG_SET, ConflictKind.BLANK_SET}
        ]
        assert staged, "the edge cases should stage a wrong and a blank set"
        out = tmp_path / "roster_sets"
        manifest = generate_dataset(
            out, build_answer_sheet_template(), seed=12, population=population,
            write_metadata=False,
        )
        keys = self._key_answers(manifest)
        sheets = population.sheets_to_render()
        for name, candidate in zip(manifest.entries, sheets, strict=True):
            truth = load_ground_truth(out / GROUND_TRUTH_DIRNAME / name)
            performance = truth.metadata["performance"]
            assert performance["answer_key_set"] == candidate.set_code
            if candidate.conflict is ConflictKind.WRONG_SET:
                assert truth.set_code != candidate.set_code
            intended = {int(n): v.upper() for n, v in performance["intended_answers"].items()}
            correct = sum(
                1 for n, v in intended.items() if keys[candidate.set_code][n] == v
            )
            assert correct == performance["target_correct"], name


class TestUnrepresentableSetCodesAreSafeDownstream:
    """Known limitation: logical sets 10/11/12 on an A-D set-code field."""

    def test_the_solution_sheet_reads_as_its_key_with_no_set_and_nothing_breaks(
        self, tmp_path: Path, engine
    ):
        from omr_scanner.services.scoring import usable_set_code

        template = build_answer_sheet_template(roll_digits=8)
        out, manifest = small(tmp_path, "logical", template=template, sets=("10", "11", "12"))
        plan = plan_for(template)
        assert [e["set_code_marked"] for e in manifest.generator["solutions"]["sets"]] == [
            False, False, False,
        ]
        for code in ("10", "11", "12"):
            scanned = key_from_scan(
                engine.process(out / SOLUTION_DIRNAME / f"Set_{code}_Solution.png", template),
                plan,
            )
            # The answers are the key; the set is honestly absent - the operator
            # must choose it, and the sheet never claims to be another set.
            assert scanned.answers == key_text(out, code).strip()
            assert scanned.set_code in {"", "_"}
            assert usable_set_code(scanned.set_code) == ""
        first = manifest.entries[0]
        result = engine.process(out / IMAGES_DIRNAME / first.replace(".json", ".png"), template)
        # A candidate's blank set code is unusable, so scoring blocks it as
        # SET_MISSING rather than borrowing any set's key.
        assert usable_set_code(result.set_code_value) == ""
