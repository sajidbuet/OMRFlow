"""The intended set code is written in the set-code boxes above its bubbles.

The same contract as the Student ID (``test_written_student_id.py``), for the
other identity field an operator resolves from the paper:

1. **The written set is the intended one, not the bubbled one** - a wrongly
   bubbled, double-marked or blank set code still shows the set the candidate
   sat.
2. **Nothing else changes** - bubbles, expected readings, every truth field,
   and the Student ID's own pixels.
3. **It is laid out the way the field spells a code** - a whole code in the one
   box of a field whose bubbles are whole codes (``10``); one character per box
   of a positional field (``05``, leading zero kept); nothing at all for a code
   the field cannot spell.

Each box is read back by matching it against every text the field could hold.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
import pytest
from tests.conftest import DIGIT_SYMBOLS, build_answer_sheet_template
from tests.integration.test_written_student_id import (
    SEED,
    cell_crop,
    page_cells,
    used_form,
)

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.reconciliation import AttendanceState
from omr_scanner.evaluation.attendance_dataset import (
    ConflictKind,
    SyntheticCandidate,
    bind_case,
    written_set,
)
from omr_scanner.evaluation.reference_scan import load_reference_scan
from omr_scanner.evaluation.synthetic_dataset import (
    PageRender,
    RenderMode,
    page_render_size,
    render_case,
    sheet_spec_from_template,
    written_set_code,
)
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder, TestCaseTag
from omr_scanner.evaluation.write_in import (
    IDENTIFIER_ROLE,
    SET_CODE_ROLE,
    WriteInRow,
    set_code_row,
    write_in_rows,
)
from omr_scanner.imaging.synthetic import (
    ColorMode,
    WrittenCharacterSpec,
    page_distortion_homography,
    render_mark_layer,
)

if TYPE_CHECKING:
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.test_cases import SheetCase


@pytest.fixture
def template() -> OmrTemplate:
    return build_answer_sheet_template(roll_digits=8)


def build_case(
    template: OmrTemplate,
    set_value: str | None,
    *,
    marks: tuple[str, ...] | None = None,
    blank: bool = False,
    index: int = 1,
) -> SheetCase:
    """A sheet that intends ``set_value``, with its bubbles optionally overridden."""
    layout = FieldLayout.of(template)
    builder = SheetBuilder(layout, index, random.Random(index))
    builder.identifier("13000016").answer_all()
    if set_value is not None:
        builder.set_code(set_value)
    if marks is not None:
        builder.set_column(0, marks)
    if blank:
        for position in range(layout.set_columns):
            builder.set_column(position, ())
    return builder.tag(TestCaseTag.BASELINE).build()


def read_row(
    image: np.ndarray,
    template: OmrTemplate,
    row: WriteInRow,
    width: int,
    height: int,
    to_image: np.ndarray,
    candidates: tuple[str, ...],
) -> str:
    """What is written in each of ``row``'s boxes, ``_`` for an empty one.

    Each box is compared with every candidate text drawn un-jittered in that
    box; the best normalised correlation wins.
    """
    identity = np.eye(3)
    read = []
    for index, cell in enumerate(page_cells(row, width, height)):
        crop = cell_crop(image, cell, to_image)
        if np.count_nonzero(crop < 128) < 0.01 * crop.size:
            read.append("_")
            continue
        box = row.cells[index]
        scores = {}
        for text in candidates:
            spec = sheet_spec_from_template(
                template, {},
                render=PageRender(width=width, height=height, dpi=0, derived_from="test"),
                written_characters=(
                    WrittenCharacterSpec(
                        center=NormalizedPoint(x=box.center_x, y=box.center_y),
                        width=box.width, height=box.height, character=text,
                    ),
                ),
            )
            glyph_crop = cell_crop(render_mark_layer(spec).image, cell, identity, inset=0.0)
            ys, xs = np.nonzero(glyph_crop < 200)
            glyph = glyph_crop[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
            if glyph.shape[0] > crop.shape[0] or glyph.shape[1] > crop.shape[1]:
                glyph = cv2.resize(glyph, (min(glyph.shape[1], crop.shape[1]),
                                           min(glyph.shape[0], crop.shape[0])))
            scores[text] = float(cv2.matchTemplate(crop, glyph, cv2.TM_CCOEFF_NORMED).max())
        best = max(scores, key=lambda text: scores[text])
        assert scores[best] > 0.5, f"box {index} holds nothing recognisable: {scores}"
        read.append(best)
    return "".join(read)


def read_set(
    template: OmrTemplate, case: SheetCase, *, dpi: int = 150, **kwargs: object
) -> tuple[str, np.ndarray, object]:
    """Render ``case`` from the template and read its set-code boxes back."""
    render = page_render_size(template, dpi)
    sheet = render_case(template, case, render=render, seed=SEED, **kwargs)
    row = set_code_row(write_in_rows(template))
    assert row is not None
    to_image, _canvas = page_distortion_homography(render.width, render.height, case.distortion)
    candidates = row.symbols if len(row.cells) == 1 else tuple(dict.fromkeys("".join(row.symbols)))
    text = read_row(sheet.image, template, row, render.width, render.height, to_image, candidates)
    return text, sheet.image, sheet.truth


# ----------------------------------------------------------------------
# What is written
# ----------------------------------------------------------------------
class TestTheIntendedSetIsWritten:
    def test_a_normal_single_character_set(self, template):
        text, _image, truth = read_set(template, build_case(template, "B"))
        assert text == "B"
        assert truth.set_code == "B"
        assert truth.metadata["written_set_code"] == "B"

    def test_a_wrongly_bubbled_set_is_still_written_correctly(self, template):
        text, _image, truth = read_set(template, build_case(template, "B", marks=("D",)))
        assert truth.set_code == "D"
        assert text == "B"
        assert truth.metadata["written_set_code"] == "B"

    def test_a_double_marked_set_is_still_written_correctly(self, template):
        text, _image, truth = read_set(template, build_case(template, "B", marks=("B", "C")))
        assert truth.set_code == "?"
        assert truth.set_marks == (("B", "C"),)
        assert text == "B"

    def test_blank_set_bubbles_still_carry_the_written_set(self, template):
        text, _image, truth = read_set(template, build_case(template, "B", blank=True))
        assert truth.set_code == "_"
        assert text == "B"

    def test_no_intended_set_leaves_the_box_empty(self, template):
        text, _image, truth = read_set(template, build_case(template, None))
        assert text == "_"
        assert truth.metadata["written_set_code"] is None

    def test_a_whole_multi_digit_code_goes_in_its_one_box(self):
        template = build_answer_sheet_template(roll_digits=8, set_symbols=("10", "11", "12"))
        text, _image, truth = read_set(template, build_case(template, "11"))
        assert truth.set_code == "11"
        assert text == "11"

    def test_a_positional_code_keeps_its_leading_zero(self):
        template = build_answer_sheet_template(
            roll_digits=8, set_symbols=DIGIT_SYMBOLS, set_positions=2
        )
        row = set_code_row(write_in_rows(template))
        assert len(row.cells) == 2
        text, _image, truth = read_set(template, build_case(template, "05"))
        assert truth.set_code == "05"
        assert text == "05"
        assert truth.metadata["written_set_code"] == "05"

    def test_a_code_the_field_cannot_spell_is_not_written(self, template):
        # "10" on an A-D field: nothing a candidate could bubble matches it,
        # and the template says nothing about what such a sheet would carry.
        text, _image, truth = read_set(template, build_case(template, "10"))
        assert text == "_"
        assert truth.metadata["written_set_code"] is None

    @pytest.mark.parametrize("mode", list(ColorMode))
    def test_every_colour_mode(self, template, mode):
        text, image, _truth = read_set(template, build_case(template, "C"), color_mode=mode)
        assert text == "C"
        if mode is ColorMode.BLACK_AND_WHITE:
            assert set(np.unique(image)) <= {0, 255}

    @pytest.mark.parametrize("dpi", [150, 300])
    def test_more_than_one_resolution(self, template, dpi):
        text, _image, _truth = read_set(template, build_case(template, "A"), dpi=dpi)
        assert text == "A"


class TestPopulationSet:
    """Which set a population's script carries, per staged conflict."""

    def candidate(self, conflict: ConflictKind, observed: str | None) -> SyntheticCandidate:
        return SyntheticCandidate(
            candidate_uid="C0007",
            roll="10000007",
            name="Synthetic Candidate 7",
            set_code="B",
            true_attendance=AttendanceState.PRESENT,
            conflict=conflict,
            observed_roll="10000007",
            observed_set=observed,
        )

    @pytest.mark.parametrize(
        ("conflict", "observed"),
        [(ConflictKind.NONE, "B"), (ConflictKind.WRONG_SET, "D"), (ConflictKind.BLANK_SET, None)],
    )
    def test_the_registered_set_is_written_whatever_is_bubbled(
        self, template, conflict, observed
    ):
        candidate = self.candidate(conflict, observed)
        assert written_set(candidate) == "B"
        case = bind_case(build_case(template, "A"), candidate, FieldLayout.of(template))
        assert case.intended_set == "B"
        assert case.set_code == (observed or "")
        text, _image, truth = read_set(template, case)
        assert text == "B"
        assert truth.metadata["written_set_code"] == "B"


# ----------------------------------------------------------------------
# Nothing else changes, and it is deterministic
# ----------------------------------------------------------------------
class TestIsolation:
    def test_the_truth_differs_only_by_the_written_record(self, template):
        case = build_case(template, "B", marks=("D",))
        with_text = render_case(template, case, seed=SEED).truth
        without = render_case(template, case, seed=SEED, render_set_code_text=False).truth
        assert "written_set_code" not in without.metadata
        metadata = dict(with_text.metadata)
        assert metadata.pop("written_set_code") == "B"
        assert replace(with_text, metadata=metadata) == without

    def test_only_the_set_code_boxes_change(self, template):
        # The Student ID - and everything else on the page - is drawn exactly
        # as it was before set codes were written.
        case = build_case(template, "B")
        render = page_render_size(template, 150)
        with_text = render_case(template, case, render=render, seed=SEED).image
        without = render_case(
            template, case, render=render, seed=SEED, render_set_code_text=False
        ).image
        changed = np.argwhere(with_text != without)
        assert changed.size
        (x0, y0, x1, y1), = page_cells(set_code_row(write_in_rows(template)),
                                       render.width, render.height)
        margin = case.distortion.margin_px
        assert changed[:, 0].min() >= round(y0) + margin - 2
        assert changed[:, 0].max() <= round(y1) + margin + 2
        assert changed[:, 1].min() >= round(x0) + margin - 2
        assert changed[:, 1].max() <= round(x1) + margin + 2

    def test_the_same_inputs_draw_the_same_pixels(self, template):
        case = build_case(template, "C")
        assert np.array_equal(render_case(template, case, seed=SEED).image,
                              render_case(template, case, seed=SEED).image)

    def test_the_writing_is_drawn_from_its_own_stream(self, template):
        rows = write_in_rows(template)
        case = build_case(template, "C")
        one = written_set_code(case, rows, seed=1, print_boxes=False).characters
        again = written_set_code(case, rows, seed=1, print_boxes=False).characters
        other = written_set_code(case, rows, seed=2, print_boxes=False).characters
        assert one == again
        assert [c.character for c in one] == [c.character for c in other] == ["C"]


# ----------------------------------------------------------------------
# On a reference scan
# ----------------------------------------------------------------------
class TestOnAReferenceScan:
    def test_written_into_the_printed_set_code_box(self, template, tmp_path: Path):
        image = used_form(template, printed_rows=(IDENTIFIER_ROLE, SET_CODE_ROLE))
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), image)
        reference = load_reference_scan(path, template)
        row = set_code_row(reference.write_in.rows)
        assert row.refined
        case = build_case(template, "D", marks=("A",))
        sheet = render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN,
                            reference=reference, seed=SEED)
        to_image, _canvas = page_distortion_homography(
            reference.width, reference.height, case.distortion
        )
        text = read_row(sheet.image, template, row, reference.canonical_width,
                        reference.canonical_height, to_image @ reference.canonical_to_scan,
                        row.symbols)
        assert text == "D"
        assert sheet.truth.set_code == "A"
        assert sheet.truth.metadata["written_set_code"] == "D"

    def test_nothing_is_written_where_the_form_prints_no_box(self, template, tmp_path: Path):
        image = used_form(template)  # identifier boxes only
        path = tmp_path / "used.png"
        cv2.imwrite(str(path), image)
        reference = load_reference_scan(path, template)
        assert reference.write_in.unprinted_rows == ("set_code",)
        case = build_case(template, "D")
        sheet = render_case(template, case, render_mode=RenderMode.REFERENCE_SCAN,
                            reference=reference, seed=SEED)
        assert "written_set_code" not in sheet.truth.metadata
        assert sheet.truth.metadata["written_student_id"] == "13000016"
