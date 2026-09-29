"""A used reference scan's old bubble marks never reach a synthetic sheet.

The failure this guards against: the repository's own sample reference has
every answer, every Student-ID ``0`` and both set-code positions filled, and
until the reference was cleaned every sheet drawn on it carried those marks as
well as its own - so its bubbles no longer read as its ground truth.

What has to be true:

1. **Only old marks go.** Every marked bubble is found, no unmarked one is
   touched, and a genuinely blank reference comes out byte-identical.
2. **The printing stays.** A cleaned bubble looks like the same bubble on the
   blank form - ring and printed label - not like a white disc.
3. **The generated sheet reads as its truth.** Checked with OMRFlow's own
   recognition engine, which reads bubbles and nothing else: with the old
   marks gone it reads exactly the synthetic candidate; with cleaning turned
   off it does not, which is what makes the test worth having.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.bubble_cleanup import _locate
from omr_scanner.evaluation.reference_scan import load_reference_scan
from omr_scanner.evaluation.synthetic_dataset import (
    PageRender,
    RenderMode,
    page_render_size,
    render_case,
    sheet_spec_from_template,
)
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder, TestCaseTag
from omr_scanner.imaging.synthetic import ColorMode, render_sheet
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.test_cases import SheetCase

SEED = 20260929
SAMPLE = Path(__file__).resolve().parents[2] / "Sample-Project" / "1.Template"


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return build_answer_sheet_template(roll_digits=8)


def used_case(template: OmrTemplate, identifier: str = "13572468") -> SheetCase:
    """The old candidate: set A, ``identifier``, and every question answered.

    The answers cycle through the options, as a real script's do, so every
    printed label still has unfilled copies elsewhere on the form.
    """
    layout = FieldLayout.of(template)
    builder = SheetBuilder(layout, 1, random.Random(1))
    builder.identifier(identifier).set_code("A")
    for number in layout.questions:
        labels = layout.labels_for(number)
        builder.answer(number, (labels[number % len(labels)],))
    return builder.build()


def new_case(template: OmrTemplate) -> SheetCase:
    """A different candidate, who leaves question 1 blank and answers the rest."""
    layout = FieldLayout.of(template)
    builder = SheetBuilder(layout, 2, random.Random(2))
    builder.identifier("24681357").set_code("C").answer_all()
    builder.blank(1)                              # old mark there: must read blank
    builder.answer(2, ("A",))                     # old mark on C: must read A
    return builder.tag(TestCaseTag.BASELINE).build()


def page(template: OmrTemplate, marks: dict, dpi: int | None = None) -> np.ndarray:
    """A printed form with ``marks`` filled in, at the canonical size or ``dpi``."""
    render = (
        page_render_size(template, dpi) if dpi else PageRender(
            width=template.page.canonical_width_px,
            height=template.page.canonical_height_px,
            dpi=150, derived_from="canonical",
        )
    )
    return render_sheet(sheet_spec_from_template(template, marks, render=render)).image


def marked_count(case: SheetCase) -> int:
    return sum(len(plan.labels) for plans in case.marks.values() for plan in plans.values())


def read(sheet_image: np.ndarray, template: OmrTemplate, tmp_path: Path, name: str):
    path = tmp_path / f"{name}.png"
    cv2.imwrite(str(path), sheet_image)
    return RecognitionEngine(RecognitionOptions(with_preview=False)).process(path, template)


def assert_reads_as_truth(result, truth) -> None:
    assert result.identifier_value == truth.roll
    assert result.set_code_value == truth.set_code
    for number, expected in truth.answers.items():
        assert result.answer(number).value == expected, f"question {number}"


# ----------------------------------------------------------------------
# 1. Only old marks go
# ----------------------------------------------------------------------
class TestWhatIsFound:
    def test_a_blank_reference_is_untouched(self, template, tmp_path: Path):
        blank = page(template, {})
        path = tmp_path / "blank.png"
        cv2.imwrite(str(path), blank)
        reference = load_reference_scan(path, template, prepare_write_in=False)
        assert reference.bubbles.cleaned == 0
        assert np.array_equal(reference.image, blank)

    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_every_old_mark_and_nothing_else(self, template, tmp_path: Path, mode):
        case = used_case(template)
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, case.marks))
        reference = load_reference_scan(path, template, color_mode=mode)
        record = reference.bubbles
        assert record.cleaned == marked_count(case)
        assert dict(record.cleaned_by_zone) == {
            zone: sum(len(p.labels) for p in plans.values())
            for zone, plans in case.marks.items()
            if any(p.labels for p in plans.values())
        }
        assert record.labels_not_restored == 0
        assert record.skipped == 0

    @pytest.mark.parametrize("dpi", [150, 300])
    def test_at_more_than_one_resolution(self, template, tmp_path: Path, dpi):
        case = used_case(template)
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, case.marks, dpi=dpi))
        reference = load_reference_scan(path, template)
        assert reference.bubbles.cleaned == marked_count(case)
        assert reference.bubbles.labels_not_restored == 0

    def test_cleaning_the_same_reference_twice_gives_the_same_pixels(
        self, template, tmp_path: Path
    ):
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, used_case(template).marks))
        first = load_reference_scan(path, template).image
        assert np.array_equal(first, load_reference_scan(path, template).image)

    def test_cleaning_can_be_turned_off(self, template, tmp_path: Path):
        used = page(template, used_case(template).marks)
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), used)
        reference = load_reference_scan(
            path, template, prepare_write_in=False, clean_bubbles=False
        )
        assert reference.bubbles is None
        assert np.array_equal(reference.image, used)


# ----------------------------------------------------------------------
# 2. The printing stays
# ----------------------------------------------------------------------
class TestThePrintingStays:
    def test_a_cleaned_bubble_looks_like_the_blank_form(self, template, tmp_path: Path):
        case = used_case(template)
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, case.marks))
        reference = load_reference_scan(path, template, prepare_write_in=False)
        blank = page(template, {})
        width, height = reference.canonical_width, reference.canonical_height
        for bubble in _locate(template, reference.canonical_to_scan, width, height):
            r = round(bubble.radius)
            window = (slice(bubble.y - r - 2, bubble.y + r + 3),
                      slice(bubble.x - r - 2, bubble.x + r + 3))
            cleaned, printed = reference.image[window], blank[window]
            difference = np.abs(cleaned.astype(int) - printed.astype(int))
            # The ring and the label are back, where the blank form has them:
            # every printed pixel is still printed, and the bubble as a whole
            # is within a few grey levels of the blank form's.
            assert np.all(cleaned[printed < 128] < 160), f"print lost at {bubble.label}"
            assert difference.mean() < 6.0, f"{bubble.zone_id} {bubble.label}"

    def test_a_label_with_no_clean_copy_is_emptied_and_counted(
        self, template, tmp_path: Path
    ):
        # Every identifier "0" filled - the sample form's case - on a form
        # whose only "0"s are those eight: there is no clean copy to restore
        # the printed label from. The fill still goes; the loss is reported.
        case = used_case(template, identifier="00000000")
        path = tmp_path / "zeros.png"
        cv2.imwrite(str(path), page(template, case.marks))
        reference = load_reference_scan(path, template, prepare_write_in=False)
        assert reference.bubbles.labels_not_restored == 8
        assert reference.bubbles.cleaned == marked_count(case)
        width, height = reference.canonical_width, reference.canonical_height
        for bubble in _locate(template, reference.canonical_to_scan, width, height):
            if bubble.zone_id == "roll_number" and bubble.label == "0":
                r = round(0.7 * bubble.radius)
                inner = reference.image[bubble.y - r : bubble.y + r + 1,
                                        bubble.x - r : bubble.x + r + 1]
                assert np.mean(inner < 128) < 0.05


# ----------------------------------------------------------------------
# 3. The generated sheet reads as its truth
# ----------------------------------------------------------------------
class TestTheSheetReadsAsItsTruth:
    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_only_the_new_candidates_marks_are_read(self, template, tmp_path: Path, mode):
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, used_case(template).marks))
        reference = load_reference_scan(path, template, color_mode=mode)
        case = new_case(template)
        sheet = render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN,
                            reference=reference, color_mode=mode, seed=SEED)
        assert sheet.truth.answers[1] == ""
        assert sheet.truth.answers[2] == "A"
        assert_reads_as_truth(read(sheet.image, template, tmp_path, "sheet"), sheet.truth)

    def test_without_cleaning_the_old_marks_leak(self, template, tmp_path: Path):
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), page(template, used_case(template).marks))
        reference = load_reference_scan(path, template, clean_bubbles=False)
        case = new_case(template)
        sheet = render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN,
                            reference=reference, seed=SEED)
        result = read(sheet.image, template, tmp_path, "leaky")
        assert result.identifier_value != sheet.truth.roll
        assert result.answer(1).value != ""


# ----------------------------------------------------------------------
# The repository's own used sample
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def sample():
    from omr_scanner.services.template_service import load_template

    if not (SAMPLE / "ECE-0000.png").exists():  # pragma: no cover - trimmed checkout
        pytest.skip("the sample project is not present")
    return load_template(SAMPLE / "BUET100q.omrt"), SAMPLE / "ECE-0000.png"


class TestTheSampleForm:
    @pytest.mark.parametrize("mode", [ColorMode.COLOR, ColorMode.GRAYSCALE])
    def test_every_filled_bubble_is_cleaned(self, sample, mode):
        template, scan = sample
        record = load_reference_scan(scan, template, color_mode=mode).bubbles
        # 100 answers, 8 identifier zeros and both set-code positions.
        assert record.cleaned == 110
        by_zone = dict(record.cleaned_by_zone)
        assert by_zone["student_id"] == 8
        assert by_zone["set_code"] == 2
        assert sum(v for k, v in by_zone.items() if k.startswith("questions")) == 100
        assert record.labels_not_restored == 0

    def test_a_new_candidate_on_it_reads_as_its_truth(self, sample, tmp_path: Path):
        template, scan = sample
        reference = load_reference_scan(scan, template, color_mode=ColorMode.COLOR)
        layout = FieldLayout.of(template)
        builder = SheetBuilder(layout, 3, random.Random(3))
        builder.identifier("24681357").set_code("12").answer_all()
        case = builder.build()
        sheet = render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN,
                            reference=reference, color_mode=ColorMode.COLOR, seed=SEED)
        assert_reads_as_truth(read(sheet.image, template, tmp_path, "sample"), sheet.truth)
