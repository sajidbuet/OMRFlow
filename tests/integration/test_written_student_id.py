"""The intended Student ID is written in the write-in boxes above the bubbles.

What has to be true, in order of importance:

1. **The written number is the intended one, not the bubbled one.** A sheet
   whose bubbles are blank, double-marked or wrong still shows the number the
   candidate meant - that is the evidence an operator resolves it with, and
   writing the bubbles' reading instead would make every such case trivially
   self-consistent.
2. **Nothing else changes.** The bubbles, the expected readings, the answers
   and every other truth field are exactly what they were without the text.
3. **It is really there, in the right boxes, in every output mode.** Checked by
   *reading* each box back - matching it against each digit's glyph - rather
   than by asserting that some ink exists somewhere.
4. **An old number on a reference scan does not survive under the new one.**

The digit reader below is deliberately simple: it only has to tell ten known
glyphs apart in a box whose position is known, which normalised
cross-correlation does reliably without any OCR dependency.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.reconciliation import AttendanceState
from omr_scanner.evaluation.attendance_dataset import (
    ConflictKind,
    SyntheticCandidate,
    bind_case,
    written_roll,
)
from omr_scanner.evaluation.reference_scan import load_reference_scan
from omr_scanner.evaluation.synthetic_dataset import (
    PageRender,
    RenderMode,
    page_render_size,
    render_case,
    sheet_spec_from_template,
    written_identifier,
)
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder, TestCaseTag
from omr_scanner.evaluation.write_in import (
    IDENTIFIER_ROLE,
    SET_CODE_ROLE,
    WriteInRow,
    identifier_row,
    write_in_rows,
)
from omr_scanner.imaging.synthetic import (
    ColorMode,
    DistortionSpec,
    PrintedBoxSpec,
    WrittenCharacterSpec,
    page_distortion_homography,
    render_mark_layer,
    render_sheet,
)
from omr_scanner.services.recognition_service import RecognitionEngine
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.test_cases import SheetCase

DIGITS = "0123456789"
SEED = 20260929
SAMPLE_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "Sample-Project" / "1.Template" / "BUET100q.omrt"
)


@pytest.fixture
def template() -> OmrTemplate:
    return build_answer_sheet_template(roll_digits=8)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def build_case(
    template: OmrTemplate,
    value: str,
    *,
    wrong_last: str | None = None,
    double_middle: tuple[str, str] | None = None,
    blank_all: bool = False,
    index: int = 1,
) -> SheetCase:
    """A sheet whose intended identifier is ``value``, with an optional defect."""
    layout = FieldLayout.of(template)
    builder = SheetBuilder(layout, index, random.Random(index))
    builder.identifier(value).set_code("A").answer_all()
    if wrong_last is not None:
        builder.identifier_column(layout.identifier_columns - 1, (wrong_last,))
    if double_middle is not None:
        builder.identifier_column(layout.identifier_columns // 2, double_middle)
    if blank_all:
        for position in range(layout.identifier_columns):
            builder.blank_identifier_column(position)
    return builder.tag(TestCaseTag.BASELINE).build()


def page_cells(
    row: WriteInRow, width: int, height: int
) -> list[tuple[float, float, float, float]]:
    """Each cell of ``row`` as ``(x0, y0, x1, y1)`` in page pixels."""
    return [
        (cell.x * width, cell.y * height, (cell.x + cell.width) * width,
         (cell.y + cell.height) * height)
        for cell in row.cells
    ]


def to_grey(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def cell_crop(
    image: np.ndarray, cell: tuple[float, float, float, float], to_image: np.ndarray,
    *, inset: float = 0.12,
) -> np.ndarray:
    """The inside of one box as it appears in ``image``, borders excluded."""
    x0, y0, x1, y1 = cell
    dx, dy = (x1 - x0) * inset, (y1 - y0) * inset
    corners = np.array([[[x0 + dx, y0 + dy]], [[x1 - dx, y1 - dy]]], dtype=np.float64)
    (ax, ay), (bx, by) = cv2.perspectiveTransform(corners, to_image).reshape(2, 2)
    return to_grey(image)[round(ay):round(by), round(ax):round(bx)]


def glyph_templates(
    row: WriteInRow, width: int, height: int
) -> dict[str, list[np.ndarray]]:
    """Each digit, drawn un-jittered in every cell, cropped to its own ink."""
    templates: dict[str, list[np.ndarray]] = {}
    identity = np.eye(3)
    for digit in DIGITS:
        spec = sheet_spec_from_template(
            build_answer_sheet_template(roll_digits=len(row.cells)),
            {},
            render=PageRender(width=width, height=height, dpi=0, derived_from="test"),
            written_characters=tuple(
                WrittenCharacterSpec(
                    center=_point(cell.center_x, cell.center_y),
                    width=cell.width,
                    height=cell.height,
                    character=digit,
                )
                for cell in row.cells
            ),
        )
        layer = render_mark_layer(spec).image
        crops = []
        for cell in page_cells(row, width, height):
            crop = cell_crop(layer, cell, identity, inset=0.0)
            ys, xs = np.nonzero(crop < 200)
            crops.append(crop[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1])
        templates[digit] = crops
    return templates


def _point(x: float, y: float) -> NormalizedPoint:
    return NormalizedPoint(x=x, y=y)


def read_written(
    image: np.ndarray,
    row: WriteInRow,
    width: int,
    height: int,
    to_image: np.ndarray,
    templates: dict[str, list[np.ndarray]],
) -> str:
    """Read the digit in each box, or ``_`` for an empty one."""
    read = []
    for index, cell in enumerate(page_cells(row, width, height)):
        crop = cell_crop(image, cell, to_image)
        if np.count_nonzero(crop < 128) < 0.01 * crop.size:
            read.append("_")
            continue
        scores = {}
        for digit in DIGITS:
            glyph = templates[digit][index]
            if glyph.shape[0] > crop.shape[0] or glyph.shape[1] > crop.shape[1]:
                glyph = cv2.resize(glyph, (min(glyph.shape[1], crop.shape[1]),
                                           min(glyph.shape[0], crop.shape[0])))
            scores[digit] = float(
                cv2.matchTemplate(crop, glyph, cv2.TM_CCOEFF_NORMED).max()
            )
        best = max(scores, key=lambda digit: scores[digit])
        assert scores[best] > 0.5, f"box {index} holds nothing recognisable: {scores}"
        read.append(best)
    return "".join(read)


def read_template_render(
    template: OmrTemplate, case: SheetCase, *, dpi: int = 150, **kwargs: object
) -> tuple[str, np.ndarray, object]:
    """Render ``case`` from the template and read its write-in boxes back."""
    render = page_render_size(template, dpi)
    sheet = render_case(template, case, render=render, seed=SEED, **kwargs)
    row = identifier_row(write_in_rows(template))
    assert row is not None
    to_image, _canvas = page_distortion_homography(render.width, render.height, case.distortion)
    text = read_written(
        sheet.image, row, render.width, render.height, to_image,
        glyph_templates(row, render.width, render.height),
    )
    return text, sheet.image, sheet.truth


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------
class TestGeometry:
    @pytest.mark.parametrize("digits", [6, 8, 10])
    def test_one_box_per_identifier_column_whatever_the_length(self, digits):
        template = build_answer_sheet_template(roll_digits=digits)
        row = identifier_row(write_in_rows(template))
        assert row is not None
        assert row.role == IDENTIFIER_ROLE
        assert len(row.cells) == digits

    def test_boxes_sit_above_their_bubble_columns_without_overlapping(self, template):
        zone = next(z for z in template.zones if z.id == "roll_number")
        grid = zone.grid
        row = identifier_row(write_in_rows(template))
        first_bubble_top = grid.origin.y - grid.bubble_size.height / 2.0
        for column, cell in enumerate(row.cells):
            assert cell.center_x == pytest.approx(grid.bubble_center(0, column).x)
            assert cell.y + cell.height < first_bubble_top
            assert cell.y > 0.0
        for left, right in zip(row.cells, row.cells[1:], strict=False):
            assert left.x + left.width <= right.x + 1e-9

    def test_the_sample_project_template_gets_a_row_for_id_and_set_code(self):
        from omr_scanner.services.template_service import load_template

        path = SAMPLE_TEMPLATE
        if not path.exists():  # pragma: no cover - a trimmed checkout
            pytest.skip("the sample project is not present")
        rows = write_in_rows(load_template(path))
        assert [row.role for row in rows] == [IDENTIFIER_ROLE, SET_CODE_ROLE]
        assert len(identifier_row(rows).cells) == 8


# ----------------------------------------------------------------------
# What is written
# ----------------------------------------------------------------------
class TestTheIntendedIdentifierIsWritten:
    def test_a_normal_sheet(self, template):
        text, _image, truth = read_template_render(template, build_case(template, "13000016"))
        assert text == "13000016"
        assert truth.roll == "13000016"
        assert truth.metadata["written_student_id"] == "13000016"

    def test_leading_zeros_are_kept(self, template):
        text, _image, truth = read_template_render(template, build_case(template, "00123456"))
        assert text == "00123456"
        assert truth.metadata["written_student_id"] == "00123456"

    @pytest.mark.parametrize(("digits", "value"), [(6, "004217"), (10, "0913000016")])
    def test_other_field_lengths(self, digits, value):
        template = build_answer_sheet_template(roll_digits=digits)
        text, _image, _truth = read_template_render(template, build_case(template, value))
        assert text == value

    def test_a_wrongly_bubbled_digit_is_still_written_correctly(self, template):
        text, _image, truth = read_template_render(
            template, build_case(template, "13000016", wrong_last="9")
        )
        assert truth.roll == "13000019"
        assert text == "13000016"

    def test_a_double_marked_column_is_still_written_correctly(self, template):
        text, _image, truth = read_template_render(
            template, build_case(template, "13000016", double_middle=("0", "8"))
        )
        assert truth.roll == "1300?016"
        assert truth.roll_marks[4] == ("0", "8")
        assert text == "13000016"

    def test_blank_bubbles_still_carry_the_written_number(self, template):
        text, _image, truth = read_template_render(
            template, build_case(template, "13000016", blank_all=True)
        )
        assert truth.roll == "________"
        assert text == "13000016"

    def test_no_intended_identifier_leaves_the_boxes_empty(self, template):
        # How a solution sheet is built: its identifier columns are blanked
        # and no identifier is ever stated, so there is nothing to write.
        layout = FieldLayout.of(template)
        builder = SheetBuilder(layout, 1, random.Random(1)).set_code("A").answer_all()
        for position in range(layout.identifier_columns):
            builder.blank_identifier_column(position)
        case = builder.build()
        assert case.intended_roll == ""
        text, _image, truth = read_template_render(template, case)
        assert text == "_" * 8
        assert truth.metadata["written_student_id"] is None


class TestPopulationIdentity:
    """Which number a population's script carries, per staged conflict."""

    def candidate(self, conflict: ConflictKind, observed: str | None) -> SyntheticCandidate:
        return SyntheticCandidate(
            candidate_uid="C0007",
            roll="10000007",
            name="Synthetic Candidate 7",
            set_code="A",
            true_attendance=AttendanceState.PRESENT,
            conflict=conflict,
            observed_roll=observed,
            observed_set="A",
        )

    @pytest.mark.parametrize(
        ("conflict", "observed"),
        [
            (ConflictKind.NONE, "10000007"),
            (ConflictKind.BLANK_CANDIDATE_ID, None),
            (ConflictKind.PARTIAL_CANDIDATE_ID, None),
            (ConflictKind.CANDIDATE_ID_MULTIPLE_MARK, None),
            (ConflictKind.WRONG_CANDIDATE_ID, "10000507"),
            (ConflictKind.DUPLICATE_SCRIPT, "10000007"),
            (ConflictKind.WRONG_SET, "10000007"),
        ],
    )
    def test_a_registered_candidate_writes_their_own_roll(self, conflict, observed):
        assert written_roll(self.candidate(conflict, observed)) == "10000007"

    def test_an_unknown_script_writes_its_own_unknown_number(self, template):
        candidate = self.candidate(ConflictKind.UNKNOWN_CANDIDATE_ID, "10009007")
        case = bind_case(build_case(template, "99999999"), candidate, FieldLayout.of(template))
        assert case.intended_roll == "10009007"
        assert case.roll == "10009007"
        text, _image, _truth = read_template_render(template, case)
        assert text == "10009007"

    def test_a_blank_bubbled_script_is_written_with_the_registered_roll(self, template):
        candidate = self.candidate(ConflictKind.BLANK_CANDIDATE_ID, None)
        case = bind_case(build_case(template, "99999999"), candidate, FieldLayout.of(template))
        assert case.roll == ""
        text, _image, _truth = read_template_render(template, case)
        assert text == "10000007"


# ----------------------------------------------------------------------
# Output modes and resolution
# ----------------------------------------------------------------------
class TestOutputModesAndResolution:
    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_the_digits_survive_every_colour_mode(self, template, mode):
        text, image, _truth = read_template_render(
            template, build_case(template, "13000016"), color_mode=mode
        )
        assert text == "13000016"
        if mode is ColorMode.COLOR:
            assert image.ndim == 3
        if mode is ColorMode.BLACK_AND_WHITE:
            assert set(np.unique(image)) <= {0, 255}

    @pytest.mark.parametrize("dpi", [150, 200, 300])
    def test_the_digits_are_legible_and_inside_their_boxes_at_any_dpi(self, template, dpi):
        case = build_case(template, "13000016")
        text, image, _truth = read_template_render(template, case, dpi=dpi)
        assert text == "13000016"

        # Nothing written outside the boxes: the band between the row and the
        # first bubble row is exactly as it is with the text turned off.
        render = page_render_size(template, dpi)
        bare = render_case(template, case, render=render, seed=SEED,
                           render_student_id_text=False).image
        row = identifier_row(write_in_rows(template))
        cells = page_cells(row, render.width, render.height)
        margin = case.distortion.margin_px
        x0, y0 = round(cells[0][0]) + margin, round(cells[0][1]) + margin
        x1, y1 = round(cells[-1][2]) + margin, round(cells[-1][3]) + margin
        changed = np.argwhere(image != bare)
        assert changed.size
        assert changed[:, 0].min() >= y0 - 2 and changed[:, 0].max() <= y1 + 2
        assert changed[:, 1].min() >= x0 - 2 and changed[:, 1].max() <= x1 + 2

    def test_the_written_text_is_degraded_with_the_page(self, template):
        case = replace(
            build_case(template, "13000016"),
            distortion=DistortionSpec(blur_kernel_px=3, noise_sigma=4.0, seed=3),
        )
        text, _image, _truth = read_template_render(template, case)
        assert text == "13000016"


# ----------------------------------------------------------------------
# Nothing else changes
# ----------------------------------------------------------------------
class TestGroundTruthAndBubblesAreUnchanged:
    def test_the_truth_differs_only_by_the_written_record(self, template):
        case = build_case(template, "13000016", wrong_last="9")
        with_text = render_case(template, case, seed=SEED).truth
        without = render_case(template, case, seed=SEED, render_student_id_text=False).truth
        assert "written_student_id" not in without.metadata
        metadata = dict(with_text.metadata)
        assert metadata.pop("written_student_id") == "13000016"
        assert replace(with_text, metadata=metadata) == without

    def test_the_engine_reads_the_bubbles_not_the_text(self, template, tmp_path: Path):
        engine = RecognitionEngine(RecognitionOptions(with_preview=False))
        case = build_case(template, "13000016", wrong_last="9")
        sheet = render_case(template, case, seed=SEED)
        path = tmp_path / "sheet.png"
        cv2.imwrite(str(path), sheet.image)
        result = engine.process(path, template)
        assert result.identifier_value == "13000019" == sheet.truth.roll
        assert result.set_code_value == sheet.truth.set_code
        for number, expected in sheet.truth.answers.items():
            assert result.answer(number).value == expected

    def test_other_random_decisions_are_untouched(self, template):
        # The jitter has its own stream: planning the same case twice, with the
        # feature on or off, draws exactly the same answers.
        first = build_case(template, "13000016")
        second = build_case(template, "13000016")
        assert first == second
        rows = write_in_rows(template)
        written = written_identifier(first, rows, seed=SEED, print_boxes=True)
        assert written.value == "13000016"
        assert build_case(template, "13000016") == first


# ----------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------
class TestDeterminism:
    def test_the_same_inputs_draw_the_same_pixels(self, template):
        case = build_case(template, "13000016")
        first = render_case(template, case, seed=SEED).image
        second = render_case(template, case, seed=SEED).image
        assert np.array_equal(first, second)

    def test_the_sheet_index_never_reaches_the_pixels(self, template):
        # A stress run's exact-duplicate scan is the same case renumbered, and
        # must stay byte-identical to its partner.
        case = build_case(template, "13000016")
        renumbered = replace(case, index=case.index + 500)
        assert np.array_equal(
            render_case(template, case, seed=SEED).image,
            render_case(template, renumbered, seed=SEED).image,
        )

    def test_a_different_seed_varies_the_handwriting_only_slightly(self, template):
        rows = write_in_rows(template)
        case = build_case(template, "13000016")
        one = written_identifier(case, rows, seed=1, print_boxes=False).characters
        two = written_identifier(case, rows, seed=2, print_boxes=False).characters
        assert [c.character for c in one] == [c.character for c in two]
        assert any(a.offset_x != b.offset_x for a, b in zip(one, two, strict=True))
        for character in (*one, *two):
            assert abs(character.offset_x) <= 0.04
            assert abs(character.offset_y) <= 0.03
            assert abs(character.size_scale - 1.0) <= 0.06


# ----------------------------------------------------------------------
# Reference scans: old handwriting is removed, borders kept
# ----------------------------------------------------------------------
class TestReferenceScanStaleText:
    @pytest.fixture
    def stale_reference(self, template, tmp_path: Path):
        """A 'blank' form that already has an old number written in its boxes."""
        render = PageRender(
            width=template.page.canonical_width_px,
            height=template.page.canonical_height_px,
            dpi=150,
            derived_from="canonical",
        )
        row = identifier_row(write_in_rows(template))
        spec = sheet_spec_from_template(
            template,
            {},
            render=render,
            write_in_boxes=tuple(
                PrintedBoxSpec(x=c.x, y=c.y, width=c.width, height=c.height)
                for c in row.cells
            ),
            written_characters=tuple(
                WrittenCharacterSpec(
                    center=_point(c.center_x, c.center_y), width=c.width, height=c.height,
                    character="8", size_scale=1.15, offset_x=0.05,
                )
                for c in row.cells
            ),
        )
        image = render_sheet(spec).image
        path = tmp_path / "used-blank.png"
        cv2.imwrite(str(path), image)
        return path, image

    def test_the_old_number_is_erased_and_the_borders_kept(self, template, stale_reference):
        path, original = stale_reference
        reference = load_reference_scan(path, template)
        preparation = reference.write_in
        assert preparation is not None
        assert preparation.erased_pixels > 0
        assert preparation.borders_found == preparation.borders_expected

        row = next(r for r in preparation.rows if r.role == IDENTIFIER_ROLE)
        assert row.refined
        width, height = reference.canonical_width, reference.canonical_height
        for cell in page_cells(row, width, height):
            crop = cell_crop(reference.image, cell, reference.canonical_to_scan, inset=0.0)
            assert np.count_nonzero(crop < 128) == 0, "old handwriting survived"

        # Every printed pixel outside the box interiors is still printed.
        inside = np.zeros(original.shape, dtype=bool)
        for x0, y0, x1, y1 in page_cells(row, width, height):
            inside[round(y0):round(y1), round(x0):round(x1)] = True
        printed = (original < 128) & ~inside
        assert np.all(reference.image[printed] < 128)

    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_the_new_number_is_all_that_is_written(self, template, stale_reference, mode):
        path, _original = stale_reference
        reference = load_reference_scan(path, template, color_mode=mode)
        row = next(r for r in reference.write_in.rows if r.role == IDENTIFIER_ROLE)
        case = build_case(template, "00123456", wrong_last="9")
        sheet = render_case(
            template, case, render_mode=RenderMode.REFERENCE_SCAN, reference=reference,
            color_mode=mode, seed=SEED,
        )
        width, height = reference.canonical_width, reference.canonical_height
        to_image, _canvas = page_distortion_homography(
            reference.width, reference.height, case.distortion
        )
        text = read_written(
            sheet.image, row, width, height, to_image @ reference.canonical_to_scan,
            glyph_templates(row, width, height),
        )
        assert text == "00123456"
        assert sheet.truth.roll == "00123459"

    def test_preparing_the_boxes_can_be_turned_off(self, template, stale_reference):
        path, original = stale_reference
        reference = load_reference_scan(path, template, prepare_write_in=False)
        assert reference.write_in is None
        assert np.array_equal(reference.image, original)
