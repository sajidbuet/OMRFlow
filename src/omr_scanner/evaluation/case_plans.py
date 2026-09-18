"""Which sheets a dataset contains, and why each one is there.

Purpose:
    Turn "give me 250 sheets in the Recognition profile" into a concrete,
    reproducible list of :class:`~omr_scanner.evaluation.test_cases.SheetCase`
    definitions - every interesting case present by construction, the rest
    filled in by seeded sampling.

Responsibilities:
    * One builder per :class:`~omr_scanner.evaluation.test_cases.CaseFamily`.
    * :func:`plan_dataset` - mandatory cases first, randomised filler after.
    * :data:`PROFILE_FAMILIES` - which families each profile draws on.

What does NOT belong here:
    * Drawing, file I/O and ground-truth writing
      (:mod:`omr_scanner.evaluation.synthetic_dataset`).
    * Any assumption about the template. Every builder asks
      :class:`~omr_scanner.evaluation.test_cases.FieldLayout` what exists and
      adapts: a template with no set code produces no set-code cases rather
      than a crash, and a template with six options exercises six.

Mandatory before random, and why it matters:
    A randomly sampled dataset of fifty sheets has a real chance of containing
    no blank identifier at all, and then the benchmark's headline number looks
    fine while the case that would have failed was never generated. Every
    family therefore emits its edge cases *first* and unconditionally; the
    randomised filler only pads a dataset out to the requested size.
"""

from __future__ import annotations

import random
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.test_cases import (
    FAINT_FILL,
    RESIDUE_FILL,
    STRONG_FILL,
    WEAK_FILL,
    CaseFamily,
    FieldLayout,
    SheetBuilder,
    SheetCase,
    TestCaseTag,
)
from omr_scanner.imaging.synthetic import DistortionSpec, MarkStyle

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    from omr_scanner.domain.template import OmrTemplate

CORNER_ROLES = ("top_left", "top_right", "bottom_right", "bottom_left")
"""Canonical corner roles, as the template and the renderer both name them."""

INTENSITY_LEVELS: tuple[float, ...] = (
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0
)
"""The fill levels the intensity sweep walks through.

Ten levels on one sheet rather than ten sheets: the sweep is about the
decision boundary, and one sheet whose questions step across it exercises the
whole range at a tenth of the cost."""


class DatasetProfile(StrEnum):
    """How a dataset is composed.

    Named for the question each answers rather than for a difficulty level -
    "does core recognition work" and "what happens to a bad scan" need
    different sheets, not more of the same ones.
    """

    BASELINE = "baseline"
    """Clean, valid sheets only. Anything failing here is a defect, not a
    tolerance question."""

    RECOGNITION = "recognition"
    """Blank and multiple marks, faint marks, erasures, mark styles - what the
    recognition rules exist for."""

    DEGRADATION = "degradation"
    """Rotation, scale, perspective, cropping, blur, noise, exposure and
    marker damage - what a scanner and a photocopier do."""

    BATCH = "batch"
    """Duplicate identifiers, mixed valid and unreadable sheets - what a batch
    of five hundred contains."""

    STRESS = "stress"
    """Controlled combinations of several defects at once."""

    MIXED = "mixed"
    """A bit of everything: the default, and the closest to a real batch."""

    CUSTOM = "custom"
    """Exactly the families the caller selected."""


PROFILE_FAMILIES: dict[DatasetProfile, tuple[CaseFamily, ...]] = {
    DatasetProfile.BASELINE: (CaseFamily.BASELINE,),
    DatasetProfile.RECOGNITION: (
        CaseFamily.BASELINE,
        CaseFamily.STUDENT_ID,
        CaseFamily.SET_CODE,
        CaseFamily.ANSWERS,
        CaseFamily.MARK_STYLES,
        CaseFamily.INTENSITY,
    ),
    DatasetProfile.DEGRADATION: (
        CaseFamily.BASELINE,
        CaseFamily.GEOMETRY,
        CaseFamily.CROPPING,
        CaseFamily.MARKERS,
        CaseFamily.IMAGE_QUALITY,
        CaseFamily.PAPER,
    ),
    DatasetProfile.BATCH: (
        CaseFamily.BASELINE,
        CaseFamily.DUPLICATES,
        CaseFamily.STUDENT_ID,
        CaseFamily.MARKERS,
    ),
    DatasetProfile.STRESS: (
        CaseFamily.MIXED,
        CaseFamily.GEOMETRY,
        CaseFamily.MARKERS,
        CaseFamily.IMAGE_QUALITY,
    ),
    DatasetProfile.MIXED: tuple(CaseFamily),
    DatasetProfile.CUSTOM: (),
}
"""Which families each profile draws on. Data rather than branching, so adding
a profile is one entry and a reader sees the whole policy at once."""


class _Planner:
    """Shared state while a dataset's cases are being built."""

    def __init__(
        self,
        layout: FieldLayout,
        rng: random.Random,
        *,
        fill_threshold: float,
        blank_threshold: float,
    ) -> None:
        self.layout = layout
        self.rng = rng
        self.fill_threshold = fill_threshold
        self.blank_threshold = blank_threshold
        self._index = 0

    def sheet(self) -> SheetBuilder:
        """Start the next sheet."""
        self._index += 1
        return SheetBuilder(self.layout, self._index, self.rng)

    # -- conveniences every family wants -------------------------------
    def identifier_value(self, offset: int = 0) -> str:
        """A fictional identifier of the right length for this template.

        Fictional by construction: derived from an index, never from anything
        resembling a real institution's numbering. Committed fixtures must not
        carry a real candidate's number.
        """
        symbols = self.layout.identifier_symbols
        columns = self.layout.identifier_columns
        if not symbols or columns <= 0:
            return ""
        value = self._index * 7919 + offset * 104729
        return "".join(
            symbols[(value // (len(symbols) ** position)) % len(symbols)]
            for position in reversed(range(columns))
        )

    def any_set(self) -> str:
        """One valid set code for this template, or ``""``."""
        if not self.layout.has_set_code:
            return ""
        if self.layout.set_columns == 1:
            return self.rng.choice(list(self.layout.set_symbols))
        return "".join(
            self.rng.choice(list(self.layout.set_symbols))
            for _ in range(self.layout.set_columns)
        )

    def valid_sheet(self, **answer_kwargs: object) -> SheetBuilder:
        """A sheet with a valid identifier, set code and every question answered."""
        builder = self.sheet()
        builder.identifier(self.identifier_value())
        builder.set_code(self.any_set())
        builder.answer_all(**answer_kwargs)
        return builder

    def question_at(self, fraction: float) -> int:
        """Return the question number at ``fraction`` through the paper."""
        questions = self.layout.questions
        if not questions:
            return 0
        position = min(int(len(questions) * fraction), len(questions) - 1)
        return questions[position]

    def expected_for_level(self, level: float) -> str:
        """Classify one intensity level against the *template's* thresholds.

        Template-driven rather than hard-coded: the bands are properties of
        the sheet design, so a template tuned differently gets a dataset whose
        expectations move with it.
        """
        if level <= self.blank_threshold:
            return "blank"
        if level < self.fill_threshold:
            return "ambiguous"
        return "marked"


# ----------------------------------------------------------------------
# Families
# ----------------------------------------------------------------------
def _baseline_cases(planner: _Planner) -> list[SheetCase]:
    """Clean, valid sheets - the regression baseline."""
    layout = planner.layout
    cases: list[SheetCase] = []

    cases.append(
        planner.valid_sheet().tag(TestCaseTag.BASELINE, TestCaseTag.ALL_ANSWERED).build()
    )

    if layout.has_questions and layout.option_labels:
        first_question = layout.questions[0]
        last_question = layout.questions[-1]
        first_option = layout.option_labels[0]
        last_option = layout.option_labels[-1]

        builder = planner.sheet()
        builder.identifier(planner.identifier_value()).set_code(planner.any_set())
        builder.blank_all().answer(first_question, (first_option,))
        cases.append(
            builder.tag(
                TestCaseTag.BASELINE, TestCaseTag.FIRST_QUESTION, TestCaseTag.FIRST_OPTION
            ).build()
        )

        builder = planner.sheet()
        builder.identifier(planner.identifier_value()).set_code(planner.any_set())
        builder.blank_all().answer(last_question, (last_option,))
        cases.append(
            builder.tag(
                TestCaseTag.BASELINE, TestCaseTag.LAST_QUESTION, TestCaseTag.LAST_OPTION
            ).build()
        )

    if layout.has_identifier:
        symbols = layout.identifier_symbols
        columns = layout.identifier_columns
        patterns = (
            (symbols[0] * columns, TestCaseTag.LOWEST_ID),
            (symbols[-1] * columns, TestCaseTag.HIGHEST_ID),
            (symbols[1 % len(symbols)] * columns, TestCaseTag.REPEATED_DIGIT_ID),
            (
                "".join(symbols[index % len(symbols)] for index in range(columns)),
                TestCaseTag.SEQUENTIAL_ID,
            ),
        )
        for value, tag in patterns:
            builder = planner.sheet()
            builder.identifier(value).set_code(planner.any_set()).answer_all()
            cases.append(builder.tag(TestCaseTag.BASELINE, tag).build())

    return cases


def _student_id_cases(planner: _Planner) -> list[SheetCase]:
    """Everything that can go wrong in the candidate identifier."""
    layout = planner.layout
    if not layout.has_identifier:
        return []

    columns = layout.identifier_columns
    symbols = layout.identifier_symbols
    middle = columns // 2
    cases: list[SheetCase] = []

    def start() -> SheetBuilder:
        builder = planner.sheet()
        builder.identifier(planner.identifier_value())
        builder.set_code(planner.any_set())
        builder.answer_all()
        return builder

    # Blank columns, in every position that matters.
    builder = start()
    for position in range(columns):
        builder.blank_identifier_column(position)
    cases.append(builder.tag(TestCaseTag.BLANK_STUDENT_ID).build())

    for position, tag in (
        (0, TestCaseTag.PARTIAL_STUDENT_ID),
        (middle, TestCaseTag.PARTIAL_STUDENT_ID),
        (columns - 1, TestCaseTag.PARTIAL_STUDENT_ID),
    ):
        builder = start()
        builder.blank_identifier_column(position)
        cases.append(builder.tag(tag).build())

    if columns >= 3:
        builder = start()
        builder.blank_identifier_column(0)
        builder.blank_identifier_column(columns - 1)
        cases.append(builder.tag(TestCaseTag.PARTIAL_STUDENT_ID).build())

    # Several bubbles in one column: two, three, and the whole column.
    for count, tag in ((2, TestCaseTag.MULTIPLE_STUDENT_ID), (3, TestCaseTag.MULTIPLE_STUDENT_ID)):
        if len(symbols) < count:
            continue
        builder = start()
        builder.identifier_column(middle, symbols[:count])
        cases.append(builder.tag(tag).build())

    builder = start()
    builder.identifier_column(middle, symbols)
    cases.append(builder.tag(TestCaseTag.MULTIPLE_STUDENT_ID).build())

    if columns >= 2 and len(symbols) >= 2:
        builder = start()
        builder.identifier_column(0, symbols[:2])
        builder.identifier_column(columns - 1, symbols[:2])
        cases.append(builder.tag(TestCaseTag.MULTIPLE_STUDENT_ID).build())

    # Faint, partial, off-centre and erased marks.
    builder = start()
    builder.identifier(planner.identifier_value(), fill=FAINT_FILL, intensity=0.5, ambiguous=True)
    cases.append(builder.tag(TestCaseTag.FAINT_STUDENT_ID).build())

    builder = start()
    builder.identifier_column(
        middle, (symbols[0],), fill=FAINT_FILL, intensity=0.5, ambiguous=True
    )
    cases.append(builder.tag(TestCaseTag.FAINT_STUDENT_ID).build())

    builder = start()
    builder.identifier(planner.identifier_value(), offset_x=0.4, offset_y=0.3)
    cases.append(builder.tag(TestCaseTag.OFFSET_STUDENT_ID).build())

    builder = start()
    builder.identifier_column(
        middle, (symbols[0],), fill=RESIDUE_FILL, intensity=0.35, residue=True
    )
    cases.append(builder.tag(TestCaseTag.ERASED_STUDENT_ID).build())

    # One firm mark and one leftover: the case a threshold gets wrong.
    if len(symbols) >= 2:
        builder = start()
        builder.identifier_column(middle, symbols[:2], fill=STRONG_FILL)
        cases.append(builder.tag(TestCaseTag.MULTIPLE_STUDENT_ID).build())

    return cases


def _set_code_cases(planner: _Planner) -> list[SheetCase]:
    """Set-code cases, skipped entirely when the template has no set code."""
    layout = planner.layout
    if not layout.has_set_code:
        return []

    symbols = layout.set_symbols
    cases: list[SheetCase] = []

    def start() -> SheetBuilder:
        builder = planner.sheet()
        builder.identifier(planner.identifier_value())
        builder.answer_all()
        return builder

    # Every valid code the template offers, capped so a 26-symbol field does
    # not dominate a small dataset.
    for symbol in symbols[:4]:
        builder = start()
        builder.set_code(symbol if layout.set_columns == 1 else symbol * layout.set_columns)
        cases.append(builder.tag(TestCaseTag.BASELINE).build())

    builder = start()
    cases.append(builder.tag(TestCaseTag.BLANK_SET_CODE).build())

    for count in (2, 3):
        if len(symbols) < count:
            continue
        builder = start()
        builder.set_column(0, symbols[:count])
        cases.append(builder.tag(TestCaseTag.MULTIPLE_SET_CODE).build())

    builder = start()
    builder.set_column(0, symbols)
    cases.append(builder.tag(TestCaseTag.MULTIPLE_SET_CODE).build())

    builder = start()
    builder.set_code(symbols[0], fill=FAINT_FILL, intensity=0.5, ambiguous=True)
    cases.append(builder.tag(TestCaseTag.FAINT_SET_CODE).build())

    builder = start()
    builder.set_code(symbols[0], fill=RESIDUE_FILL, intensity=0.35, residue=True)
    cases.append(builder.tag(TestCaseTag.ERASED_SET_CODE).build())

    if layout.set_columns > 1:
        # A multi-position code with one blank position, and one ambiguous.
        builder = start()
        builder.set_code(symbols[0] * layout.set_columns)
        builder.set_column(layout.set_columns - 1, ())
        cases.append(builder.tag(TestCaseTag.BLANK_SET_CODE).build())

        builder = start()
        builder.set_code(symbols[0] * layout.set_columns)
        builder.set_column(0, symbols[:2])
        cases.append(builder.tag(TestCaseTag.MULTIPLE_SET_CODE).build())

    return cases


def _answer_cases(planner: _Planner) -> list[SheetCase]:
    """Blank, multiple, faint, erased and misplaced answers."""
    layout = planner.layout
    if not layout.has_questions:
        return []

    labels = layout.option_labels
    questions = layout.questions
    cases: list[SheetCase] = []

    def start() -> SheetBuilder:
        builder = planner.sheet()
        builder.identifier(planner.identifier_value())
        builder.set_code(planner.any_set())
        builder.answer_all()
        return builder

    # Blanks: one, several consecutive, the first, the last, and all.
    builder = start()
    builder.blank(planner.question_at(0.5))
    cases.append(builder.tag(TestCaseTag.BLANK_QUESTION).build())

    builder = start()
    for offset in range(min(3, len(questions))):
        builder.blank(questions[offset])
    cases.append(
        builder.tag(TestCaseTag.BLANK_QUESTION, TestCaseTag.CONSECUTIVE_BLANKS).build()
    )

    builder = start()
    builder.blank(questions[0])
    cases.append(builder.tag(TestCaseTag.BLANK_QUESTION, TestCaseTag.FIRST_QUESTION).build())

    builder = start()
    builder.blank(questions[-1])
    cases.append(builder.tag(TestCaseTag.BLANK_QUESTION, TestCaseTag.LAST_QUESTION).build())

    builder = planner.sheet()
    builder.identifier(planner.identifier_value()).set_code(planner.any_set()).blank_all()
    cases.append(builder.tag(TestCaseTag.ALL_BLANK, TestCaseTag.BLANK_QUESTION).build())

    # Every pair of options the template offers, spread across the paper.
    if len(labels) >= 2:
        pairs = [
            (labels[first], labels[second])
            for first in range(len(labels))
            for second in range(first + 1, len(labels))
        ]
        builder = start()
        for position, pair in enumerate(pairs):
            number = questions[position % len(questions)]
            builder.answer(number, pair)
        cases.append(builder.tag(TestCaseTag.MULTIPLE_ANSWER).build())

    if len(labels) >= 3:
        builder = start()
        builder.answer(planner.question_at(0.25), labels[:3])
        cases.append(
            builder.tag(TestCaseTag.MULTIPLE_ANSWER, TestCaseTag.TRIPLE_ANSWER).build()
        )

    builder = start()
    builder.answer(planner.question_at(0.75), labels)
    cases.append(builder.tag(TestCaseTag.MULTIPLE_ANSWER, TestCaseTag.ALL_OPTIONS).build())

    # A firm answer with a lighter competitor beside it - the pair a threshold
    # decides, and the one worth watching when a threshold changes.
    if len(labels) >= 2:
        builder = start()
        builder.answer(planner.question_at(0.4), (labels[0],), fill=STRONG_FILL)
        builder.answer(planner.question_at(0.4), labels[:2], fill=WEAK_FILL, ambiguous=True)
        cases.append(builder.tag(TestCaseTag.STRONG_PLUS_WEAK).build())

    # Faint, erased, off-centre, oversized, undersized, between two bubbles -
    # each on its own sheet, at a different place on the paper, so a failure
    # names one cause rather than a mixture.
    variations: tuple[tuple[TestCaseTag, float, dict[str, object]], ...] = (
        (TestCaseTag.FAINT_MARK, 0.2, {"fill": FAINT_FILL, "intensity": 0.5, "ambiguous": True}),
        (TestCaseTag.ERASED_MARK, 0.35, {"fill": RESIDUE_FILL, "intensity": 0.35, "residue": True}),
        (TestCaseTag.OFFSET_MARK, 0.5, {"offset_x": 0.45, "offset_y": 0.35}),
        (TestCaseTag.OVERSIZED_MARK, 0.65, {"size_scale": 1.6}),
        (TestCaseTag.UNDERSIZED_MARK, 0.8, {"size_scale": 0.5}),
        (TestCaseTag.MARK_BETWEEN_BUBBLES, 0.9, {"offset_x": 0.95, "ambiguous": True}),
    )
    for tag, fraction, options in variations:
        builder = start()
        number = planner.question_at(fraction)
        builder.answer(number, (labels[0],), **options)
        cases.append(builder.tag(tag).build())

    # An erased A followed by a firm B: what a candidate who changed their
    # mind leaves behind.
    if len(labels) >= 2:
        builder = start()
        number = planner.question_at(0.55)
        builder.answer(number, (labels[1],), fill=STRONG_FILL)
        cases.append(builder.tag(TestCaseTag.ERASED_MARK).build())

    return cases


def _mark_style_cases(planner: _Planner) -> list[SheetCase]:
    """One sheet per way of making a mark."""
    layout = planner.layout
    if not layout.has_questions or not layout.option_labels:
        return []

    styles: tuple[tuple[MarkStyle, TestCaseTag], ...] = (
        (MarkStyle.TICK, TestCaseTag.MARK_STYLE_TICK),
        (MarkStyle.CROSS, TestCaseTag.MARK_STYLE_CROSS),
        (MarkStyle.RING, TestCaseTag.MARK_STYLE_RING),
        (MarkStyle.SCRIBBLE, TestCaseTag.MARK_STYLE_SCRIBBLE),
        (MarkStyle.DOT, TestCaseTag.MARK_STYLE_DOT),
        (MarkStyle.STROKE_H, TestCaseTag.MARK_STYLE_STROKE),
        (MarkStyle.STROKE_V, TestCaseTag.MARK_STYLE_STROKE),
        (MarkStyle.SLASH, TestCaseTag.MARK_STYLE_SLASH),
    )

    cases: list[SheetCase] = []
    for style, tag in styles:
        builder = planner.sheet()
        builder.identifier(planner.identifier_value())
        builder.set_code(planner.any_set())
        # A whole sheet in one style: the benchmark's per-category line then
        # reads "ticks 89/100", which is the number that tells a developer
        # where a coverage-based measurement is losing marks.
        builder.answer_all(style=style, fill=STRONG_FILL)
        builder.notes = f"Every answer drawn as {style.value}."
        cases.append(builder.tag(tag).build())
    return cases


def _intensity_cases(planner: _Planner) -> list[SheetCase]:
    """One sheet stepping across the decision boundary."""
    layout = planner.layout
    if not layout.has_questions or not layout.option_labels:
        return []

    label = layout.option_labels[0]
    builder = planner.sheet()
    builder.identifier(planner.identifier_value())
    builder.set_code(planner.any_set())

    for position, number in enumerate(layout.questions):
        level = INTENSITY_LEVELS[position % len(INTENSITY_LEVELS)]
        expectation = planner.expected_for_level(level)
        builder.answer(
            number,
            (label,),
            fill=level,
            intensity=0.4 + 0.6 * level,
            ambiguous=expectation == "ambiguous",
            residue=expectation == "blank",
        )
    builder.notes = (
        "Fill levels step through "
        f"{', '.join(f'{level:.0%}' for level in INTENSITY_LEVELS)}; "
        "each question's expectation comes from the template's own thresholds."
    )
    return [builder.tag(TestCaseTag.INTENSITY_SWEEP).build()]


def _geometry_cases(planner: _Planner) -> list[SheetCase]:
    """Rotation, quarter turns, scale, translation and perspective."""
    cases: list[SheetCase] = []

    rotations: tuple[tuple[float, TestCaseTag], ...] = (
        (0.5, TestCaseTag.ROTATION_MILD),
        (1.0, TestCaseTag.ROTATION_MILD),
        (2.0, TestCaseTag.ROTATION_MILD),
        (3.0, TestCaseTag.ROTATION_MODERATE),
        (5.0, TestCaseTag.ROTATION_MODERATE),
        (10.0, TestCaseTag.ROTATION_SEVERE),
        (-10.0, TestCaseTag.ROTATION_SEVERE),
    )
    for degrees, tag in rotations:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(tag).build(
                distortion=DistortionSpec(rotation_degrees=degrees, seed=builder.index)
            )
        )

    for quarter in (90.0, 180.0, 270.0):
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(TestCaseTag.QUARTER_TURN).build(
                distortion=DistortionSpec(rotation_degrees=quarter, seed=builder.index)
            )
        )

    for factor in (0.90, 0.95, 1.05, 1.10):
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(TestCaseTag.SCALE).build(
                distortion=DistortionSpec(
                    scale_x=factor, scale_y=factor, seed=builder.index
                )
            )
        )

    shifts: tuple[tuple[float, float], ...] = (
        (-60.0, 0.0), (60.0, 0.0), (0.0, -60.0), (0.0, 60.0), (45.0, 45.0)
    )
    for dx, dy in shifts:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(TestCaseTag.TRANSLATION).build(
                distortion=DistortionSpec(
                    translate_x_px=dx, translate_y_px=dy, seed=builder.index
                )
            )
        )

    perspectives: tuple[tuple[float, TestCaseTag], ...] = (
        (0.008, TestCaseTag.PERSPECTIVE_MILD),
        (0.02, TestCaseTag.PERSPECTIVE_MODERATE),
        (0.04, TestCaseTag.PERSPECTIVE_SEVERE),
    )
    for strength, tag in perspectives:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(tag).build(
                distortion=DistortionSpec(
                    perspective_strength=strength, seed=builder.index
                )
            )
        )
    return cases


def _cropping_cases(planner: _Planner) -> list[SheetCase]:
    """Pages that did not entirely make it onto the platen.

    A negative margin shrinks the canvas around the page, and a translation
    decides which edge loses most - which is how a real sheet is cropped: on
    one side, because it was fed crooked.
    """
    cases: list[SheetCase] = []

    mild: tuple[tuple[float, float], ...] = ((25.0, 0.0), (-25.0, 0.0), (0.0, 25.0), (0.0, -25.0))
    for dx, dy in mild:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(TestCaseTag.CROP_MILD).build(
                distortion=DistortionSpec(
                    margin_px=-20, translate_x_px=dx, translate_y_px=dy, seed=builder.index
                )
            )
        )

    # Far enough in to cut a corner marker: the engine should refuse rather
    # than rectify a page it cannot see the corners of.
    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.CROP_SEVERE, TestCaseTag.EXPECTED_FAILURE).build(
            distortion=DistortionSpec(margin_px=-140, seed=builder.index),
            expect_failure=True,
            notes="Cropped past the corner markers; registration must fail.",
        )
    )
    return cases


def _marker_cases(planner: _Planner) -> list[SheetCase]:
    """Registration and orientation marks that are not what they should be."""
    cases: list[SheetCase] = []

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.MARKER_FAINT).build(faint_markers=(CORNER_ROLES[1],))
    )

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.MARKER_DAMAGED).build(damaged_markers=(CORNER_ROLES[2],))
    )

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.MARKER_MISSING, TestCaseTag.EXPECTED_FAILURE).build(
            omit_markers=(CORNER_ROLES[1],),
            expect_failure=True,
            notes="One corner marker absent; a missing corner is never extrapolated.",
        )
    )

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.MARKERS_MISSING_MANY, TestCaseTag.EXPECTED_FAILURE).build(
            omit_markers=(CORNER_ROLES[0], CORNER_ROLES[2]),
            expect_failure=True,
        )
    )

    builder = planner.valid_sheet()
    cases.append(builder.tag(TestCaseTag.MARKER_EXTRA).build(extra_marker=True))

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(TestCaseTag.ORIENTATION_MISSING, TestCaseTag.EXPECTED_FAILURE).build(
            omit_orientation=True,
            expect_failure=True,
            notes="No orientation mark; which way up the page is cannot be established.",
        )
    )

    builder = planner.valid_sheet()
    cases.append(builder.tag(TestCaseTag.ORIENTATION_FAINT).build(faint_orientation=True))
    return cases


def _distortion(seed: int, options: Mapping[str, Any]) -> DistortionSpec:
    """Build a distortion from a named parameter table.

    The tables below read as "this tag means this much of this defect", which
    is the point: a reviewer can see the whole degradation ladder at once
    instead of reading fifteen constructor calls.
    """
    return replace(DistortionSpec(seed=seed), **options)


def _image_quality_cases(planner: _Planner) -> list[SheetCase]:
    """Exposure, focus, noise and compression."""
    cases: list[SheetCase] = []

    settings: tuple[tuple[TestCaseTag, dict[str, Any]], ...] = (
        (TestCaseTag.BLUR, {"blur_kernel_px": 3}),
        (TestCaseTag.BLUR, {"blur_kernel_px": 7}),
        (TestCaseTag.NOISE, {"noise_sigma": 6.0}),
        (TestCaseTag.NOISE, {"noise_sigma": 12.0}),
        (TestCaseTag.SPECKLE, {"speckle_density": 0.004}),
        (TestCaseTag.BRIGHTNESS, {"brightness_gain": 1.25}),
        (TestCaseTag.BRIGHTNESS, {"brightness_gain": 0.75}),
        (TestCaseTag.CONTRAST, {"brightness_gain": 0.6, "brightness_offset": 60.0}),
        (TestCaseTag.ILLUMINATION, {"illumination_gradient": 0.35}),
        (TestCaseTag.JPEG_ARTIFACTS, {"jpeg_quality": 40}),
    )
    for tag, options in settings:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(tag).build(distortion=_distortion(builder.index, options))
        )
    return cases


def _paper_cases(planner: _Planner) -> list[SheetCase]:
    """Paper stock and scanner artefacts."""
    cases: list[SheetCase] = []

    settings: tuple[tuple[TestCaseTag, dict[str, Any]], ...] = (
        (TestCaseTag.PAPER_TINT, {"paper_gray": 225}),
        (TestCaseTag.PAPER_TINT, {"paper_gray": 200}),
        (TestCaseTag.SCANNER_STREAK, {"streak_strength": 0.35}),
        (TestCaseTag.EDGE_SHADOW, {"edge_shadow": 0.35}),
    )
    for tag, options in settings:
        builder = planner.valid_sheet()
        cases.append(
            builder.tag(tag).build(distortion=_distortion(builder.index, options))
        )
    return cases


def _duplicate_cases(planner: _Planner) -> list[SheetCase]:
    """Several sheets claiming the same identifier.

    A batch-level condition, not a recognition failure: the engine is
    *correct* to read the same number twice, and what is being tested is that
    the batch keeps both scripts and names them apart.
    """
    layout = planner.layout
    if not layout.has_identifier:
        return []

    cases: list[SheetCase] = []

    def duplicate(value: str, tags: Sequence[TestCaseTag], **kwargs: object) -> SheetCase:
        builder = planner.sheet()
        builder.identifier(value, **kwargs)
        builder.set_code(planner.any_set())
        builder.answer_all()
        builder.tag(TestCaseTag.DUPLICATE_ID, *tags)
        return builder.build(duplicate_group=value)

    adjacent = planner.identifier_value(offset=1)
    for _ in range(2):
        cases.append(duplicate(adjacent, (TestCaseTag.DUPLICATE_ADJACENT,)))

    triple = planner.identifier_value(offset=2)
    for _ in range(3):
        cases.append(duplicate(triple, (TestCaseTag.DUPLICATE_ADJACENT,)))

    # A group of five, separated by other sheets, so the allocator is not
    # merely handling a consecutive run.
    separated = planner.identifier_value(offset=3)
    for position in range(5):
        cases.append(duplicate(separated, (TestCaseTag.DUPLICATE_SEPARATED,)))
        if position < 4:
            builder = planner.valid_sheet()
            cases.append(builder.tag(TestCaseTag.BASELINE).build())

    # Same identifier, different set codes - two different papers, one number.
    if layout.has_set_code and len(layout.set_symbols) >= 2:
        shared = planner.identifier_value(offset=4)
        for symbol in layout.set_symbols[:2]:
            builder = planner.sheet()
            builder.identifier(shared)
            builder.set_code(symbol if layout.set_columns == 1 else symbol * layout.set_columns)
            builder.answer_all()
            builder.tag(TestCaseTag.DUPLICATE_ID, TestCaseTag.DUPLICATE_DIFFERENT_SET)
            cases.append(builder.build(duplicate_group=shared))

    # One firm copy and one whose identifier is borderline: the pair where a
    # naive duplicate check and a careful one disagree.
    borderline = planner.identifier_value(offset=5)
    cases.append(duplicate(borderline, (TestCaseTag.DUPLICATE_LOW_CONFIDENCE,)))
    cases.append(
        duplicate(
            borderline,
            (TestCaseTag.DUPLICATE_LOW_CONFIDENCE, TestCaseTag.FAINT_STUDENT_ID),
            fill=FAINT_FILL,
            intensity=0.5,
            ambiguous=True,
        )
    )
    return cases


def _mixed_cases(planner: _Planner) -> list[SheetCase]:
    """Several defects at once, in the combinations that actually co-occur."""
    layout = planner.layout
    cases: list[SheetCase] = []
    labels = layout.option_labels

    if labels:
        builder = planner.valid_sheet()
        builder.answer(planner.question_at(0.3), (labels[0],), fill=FAINT_FILL, ambiguous=True)
        cases.append(
            builder.tag(
                TestCaseTag.MIXED_DEFECTS, TestCaseTag.ROTATION_MODERATE, TestCaseTag.FAINT_MARK
            ).build(
                distortion=DistortionSpec(
                    rotation_degrees=4.0, brightness_gain=0.8, seed=builder.index
                )
            )
        )

    if layout.has_identifier and labels:
        builder = planner.valid_sheet()
        builder.blank_identifier_column(0)
        builder.answer(planner.question_at(0.6), labels[:2])
        cases.append(
            builder.tag(
                TestCaseTag.MIXED_DEFECTS,
                TestCaseTag.PERSPECTIVE_MODERATE,
                TestCaseTag.PARTIAL_STUDENT_ID,
                TestCaseTag.MULTIPLE_ANSWER,
            ).build(
                distortion=DistortionSpec(perspective_strength=0.02, seed=builder.index)
            )
        )

    if labels:
        builder = planner.valid_sheet()
        builder.answer(
            planner.question_at(0.45), (labels[0],), fill=RESIDUE_FILL, residue=True
        )
        cases.append(
            builder.tag(
                TestCaseTag.MIXED_DEFECTS, TestCaseTag.BLUR, TestCaseTag.ERASED_MARK,
                TestCaseTag.MARKER_DAMAGED,
            ).build(
                distortion=DistortionSpec(blur_kernel_px=5, seed=builder.index),
                damaged_markers=(CORNER_ROLES[3],),
            )
        )

    builder = planner.valid_sheet()
    cases.append(
        builder.tag(
            TestCaseTag.MIXED_DEFECTS, TestCaseTag.NOISE, TestCaseTag.ILLUMINATION,
            TestCaseTag.SCALE,
        ).build(
            distortion=DistortionSpec(
                noise_sigma=8.0,
                illumination_gradient=0.3,
                scale_x=0.94,
                scale_y=0.94,
                seed=builder.index,
            )
        )
    )
    return cases


FAMILY_BUILDERS: dict[CaseFamily, Callable[[_Planner], list[SheetCase]]] = {
    CaseFamily.BASELINE: _baseline_cases,
    CaseFamily.STUDENT_ID: _student_id_cases,
    CaseFamily.SET_CODE: _set_code_cases,
    CaseFamily.ANSWERS: _answer_cases,
    CaseFamily.MARK_STYLES: _mark_style_cases,
    CaseFamily.INTENSITY: _intensity_cases,
    CaseFamily.GEOMETRY: _geometry_cases,
    CaseFamily.CROPPING: _cropping_cases,
    CaseFamily.MARKERS: _marker_cases,
    CaseFamily.IMAGE_QUALITY: _image_quality_cases,
    CaseFamily.PAPER: _paper_cases,
    CaseFamily.DUPLICATES: _duplicate_cases,
    CaseFamily.MIXED: _mixed_cases,
}
"""Every family's builder, keyed by family. Used by both the profiles and the
custom selection, so the two can never drift apart."""


def families_for(
    profile: DatasetProfile, custom: Sequence[CaseFamily] | None = None
) -> tuple[CaseFamily, ...]:
    """Return the families a profile draws on.

    Args:
        profile: The chosen profile.
        custom: Families selected by hand, used when the profile is
            :attr:`DatasetProfile.CUSTOM`. An empty custom selection falls back
            to the baseline, because a dataset of nothing helps nobody.
    """
    if profile is DatasetProfile.CUSTOM:
        return tuple(custom) if custom else (CaseFamily.BASELINE,)
    return PROFILE_FAMILIES[profile]


def plan_dataset(
    template: OmrTemplate,
    *,
    count: int,
    seed: int,
    profile: DatasetProfile = DatasetProfile.MIXED,
    custom_families: Sequence[CaseFamily] | None = None,
) -> list[SheetCase]:
    """Return the cases a dataset should contain, in order.

    Args:
        template: The template every sheet is built from.
        count: How many sheets are wanted.
        seed: Master seed; the same seed, count, profile and template give the
            same plan.
        profile: Which families to draw on.
        custom_families: Families to use when ``profile`` is ``CUSTOM``.

    Returns:
        Exactly ``count`` cases when the families can supply them. The
        mandatory edge cases come first, so a dataset too small to hold them
        all is a *prefix* of the interesting ones rather than a random sample
        that quietly omits the blank-identifier test.

    Raises:
        ValueError: ``count`` is not positive.
    """
    if count < 1:
        raise ValueError(f"A dataset needs at least one sheet, got {count}")

    layout = FieldLayout.of(template)
    settings = template.recognition
    rng = random.Random(seed)
    planner = _Planner(
        layout,
        rng,
        fill_threshold=settings.fill_ratio_threshold,
        blank_threshold=settings.blank_ratio_threshold,
    )

    families = families_for(profile, custom_families)
    by_family = [FAMILY_BUILDERS[family](planner) for family in families]
    mandatory: list[SheetCase] = [case for produced in by_family for case in produced]

    if len(mandatory) >= count:
        # The dataset is too small to hold every family's edge cases, so take a
        # spread across the families rather than the first N in family order -
        # otherwise a twelve-sheet Mixed dataset would be nothing but the
        # baseline and identifier families, and would look clean while testing
        # none of the degradation the profile was chosen for.
        return _renumber(_spread(by_family, count))

    # Filler: more of the same families, sampled with the dataset's own seed.
    # Never a different *kind* of sheet - a dataset should not contain cases
    # its profile did not ask for.
    filler: list[SheetCase] = []
    while len(mandatory) + len(filler) < count:
        family = rng.choice(list(families))
        produced = FAMILY_BUILDERS[family](planner)
        if not produced:
            # A family this template cannot exercise (no set code, say).
            # Fall back to a valid sheet rather than looping forever.
            produced = [planner.valid_sheet().tag(TestCaseTag.BASELINE).build()]
        rng.shuffle(produced)
        filler.extend(produced)

    return _renumber([*mandatory, *filler][:count])


def _spread(by_family: Sequence[Sequence[SheetCase]], count: int) -> list[SheetCase]:
    """Take ``count`` cases round-robin across the families.

    Each family's own cases keep their declared order, so the first case taken
    from a family is still its most important one; families simply take turns.
    A family with fewer cases than the others drops out when it is exhausted
    rather than holding up the rotation.
    """
    chosen: list[SheetCase] = []
    position = 0
    while len(chosen) < count:
        drew = False
        for produced in by_family:
            if position < len(produced):
                chosen.append(produced[position])
                drew = True
                if len(chosen) == count:
                    return chosen
        if not drew:
            break
        position += 1
    return chosen


def _renumber(cases: Sequence[SheetCase]) -> list[SheetCase]:
    """Give the chosen cases consecutive indices from one.

    The planner's own counter runs ahead of the dataset when a family is built
    and then truncated, and a dataset whose files jump from 000004 to 000011
    invites the question "where are the others?".
    """
    from dataclasses import replace

    return [replace(case, index=position) for position, case in enumerate(cases, start=1)]


__all__ = [
    "CORNER_ROLES",
    "FAMILY_BUILDERS",
    "INTENSITY_LEVELS",
    "PROFILE_FAMILIES",
    "DatasetProfile",
    "families_for",
    "plan_dataset",
]
