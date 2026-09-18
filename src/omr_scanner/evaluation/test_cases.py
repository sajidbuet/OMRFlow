"""Named synthetic test cases, planned from a template.

Purpose:
    Decide *what each generated sheet is a test of* - a blank identifier
    column, two marks on one question, a five-degree rotation, a damaged
    corner marker - and say so in a tag that survives all the way into the
    benchmark's category table. "97.9% correct" is not actionable; "ticks
    89/100, perspective 84/100, everything else perfect" is.

Responsibilities:
    * :class:`TestCaseTag` - the vocabulary of what can be tested.
    * :class:`FieldLayout` - what *this* template actually offers, so a
      generator never hard-codes seven digits or four options.
    * :class:`SheetCase` - one sheet's full definition: the marks to draw, the
      degradation to apply, the truth to record, and the tags it carries.
    * :class:`SheetBuilder` - states a mark once and derives the truth from it.

What does NOT belong here:
    * Choosing which cases a dataset contains, which is
      :mod:`omr_scanner.evaluation.case_plans`.
    * Drawing and file I/O, which are
      :mod:`omr_scanner.evaluation.synthetic_dataset`.
    * Recognition, or any opinion about it.

Why ground truth is *derived* from the marks:
    Every expected value on a case is computed from the marks the case will
    draw, by one rule, in one place. A generator that decided "this sheet
    answers B" separately from "draw a mark on B" will eventually disagree
    with itself, and the benchmark will blame the engine. This has already
    happened once in this repository (double marks were labelled in the order
    they were drawn rather than printed order), which is why the two are no
    longer allowed to be stated twice.

Mandatory before random:
    A profile's interesting cases are emitted first and unconditionally. If
    twenty sheets are asked for and the profile has thirty edge cases, the
    first twenty are edge cases - never a random sample that happens to omit
    the blank-identifier test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.template import (
    GridFieldDefinition,
    IgnoredFieldDefinition,
    QuestionBlockFieldDefinition,
)
from omr_scanner.imaging.synthetic import DistortionSpec, MarkStyle
from omr_scanner.recognition.fields import zone_groups
from omr_scanner.recognition.models import (
    BLANK_CHARACTER,
    MULTIPLE_MARK_SEPARATOR,
    UNRESOLVED_CHARACTER,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate, Zone

STRONG_FILL = 0.95
"""Coverage of a mark a candidate meant to make. Well above any threshold."""

FAINT_FILL = 0.35
"""Coverage of a deliberately borderline mark.

Between the template defaults' blank threshold (0.25) and fill threshold
(0.55), which is precisely the band where the engine is supposed to *flag* a
question rather than answer it."""

WEAK_FILL = 0.45
"""The lighter half of a strong-plus-weak competing pair."""

RESIDUE_FILL = 0.18
"""What an erased pencil mark leaves behind. Below the blank threshold: the
engine should read the bubble as empty, and a dataset that demanded otherwise
would be asking it to read erasures."""


class TestCaseTag(StrEnum):
    """What one generated sheet is a test of.

    A sheet carries several. The benchmark groups its results by these, which
    is the whole point of having them: a single overall accuracy figure hides
    the one category that has regressed.
    """

    __test__ = False
    """Not a pytest class. The name is right for what this is, and a test
    module that imports it should not have to rename it."""

    # --- baseline -----------------------------------------------------
    BASELINE = "BASELINE"
    ALL_ANSWERED = "ALL_ANSWERED"
    FIRST_QUESTION = "FIRST_QUESTION"
    LAST_QUESTION = "LAST_QUESTION"
    FIRST_OPTION = "FIRST_OPTION"
    LAST_OPTION = "LAST_OPTION"
    REPEATED_DIGIT_ID = "REPEATED_DIGIT_ID"
    SEQUENTIAL_ID = "SEQUENTIAL_ID"
    LOWEST_ID = "LOWEST_ID"
    HIGHEST_ID = "HIGHEST_ID"

    # --- identifier ---------------------------------------------------
    BLANK_STUDENT_ID = "BLANK_STUDENT_ID"
    PARTIAL_STUDENT_ID = "PARTIAL_STUDENT_ID"
    MULTIPLE_STUDENT_ID = "MULTIPLE_STUDENT_ID"
    FAINT_STUDENT_ID = "FAINT_STUDENT_ID"
    ERASED_STUDENT_ID = "ERASED_STUDENT_ID"
    OFFSET_STUDENT_ID = "OFFSET_STUDENT_ID"

    # --- set code -----------------------------------------------------
    BLANK_SET_CODE = "BLANK_SET_CODE"
    MULTIPLE_SET_CODE = "MULTIPLE_SET_CODE"
    FAINT_SET_CODE = "FAINT_SET_CODE"
    ERASED_SET_CODE = "ERASED_SET_CODE"

    # --- answers ------------------------------------------------------
    BLANK_QUESTION = "BLANK_QUESTION"
    ALL_BLANK = "ALL_BLANK"
    CONSECUTIVE_BLANKS = "CONSECUTIVE_BLANKS"
    MULTIPLE_ANSWER = "MULTIPLE_ANSWER"
    TRIPLE_ANSWER = "TRIPLE_ANSWER"
    ALL_OPTIONS = "ALL_OPTIONS"
    STRONG_PLUS_WEAK = "STRONG_PLUS_WEAK"
    FAINT_MARK = "FAINT_MARK"
    ERASED_MARK = "ERASED_MARK"
    OFFSET_MARK = "OFFSET_MARK"
    OVERSIZED_MARK = "OVERSIZED_MARK"
    UNDERSIZED_MARK = "UNDERSIZED_MARK"
    MARK_BETWEEN_BUBBLES = "MARK_BETWEEN_BUBBLES"

    # --- mark styles --------------------------------------------------
    MARK_STYLE_TICK = "MARK_STYLE_TICK"
    MARK_STYLE_CROSS = "MARK_STYLE_CROSS"
    MARK_STYLE_RING = "MARK_STYLE_RING"
    MARK_STYLE_SCRIBBLE = "MARK_STYLE_SCRIBBLE"
    MARK_STYLE_DOT = "MARK_STYLE_DOT"
    MARK_STYLE_STROKE = "MARK_STYLE_STROKE"
    MARK_STYLE_SLASH = "MARK_STYLE_SLASH"
    INTENSITY_SWEEP = "INTENSITY_SWEEP"

    # --- geometry -----------------------------------------------------
    ROTATION_MILD = "ROTATION_MILD"
    ROTATION_MODERATE = "ROTATION_MODERATE"
    ROTATION_SEVERE = "ROTATION_SEVERE"
    QUARTER_TURN = "QUARTER_TURN"
    SCALE = "SCALE"
    TRANSLATION = "TRANSLATION"
    PERSPECTIVE_MILD = "PERSPECTIVE_MILD"
    PERSPECTIVE_MODERATE = "PERSPECTIVE_MODERATE"
    PERSPECTIVE_SEVERE = "PERSPECTIVE_SEVERE"
    CROP_MILD = "CROP_MILD"
    CROP_SEVERE = "CROP_SEVERE"

    # --- markers ------------------------------------------------------
    MARKER_FAINT = "MARKER_FAINT"
    MARKER_DAMAGED = "MARKER_DAMAGED"
    MARKER_MISSING = "MARKER_MISSING"
    MARKERS_MISSING_MANY = "MARKERS_MISSING_MANY"
    MARKER_EXTRA = "MARKER_EXTRA"
    ORIENTATION_MISSING = "ORIENTATION_MISSING"
    ORIENTATION_FAINT = "ORIENTATION_FAINT"

    # --- image quality ------------------------------------------------
    BLUR = "BLUR"
    NOISE = "NOISE"
    SPECKLE = "SPECKLE"
    BRIGHTNESS = "BRIGHTNESS"
    CONTRAST = "CONTRAST"
    ILLUMINATION = "ILLUMINATION"
    JPEG_ARTIFACTS = "JPEG_ARTIFACTS"
    PAPER_TINT = "PAPER_TINT"
    SCANNER_STREAK = "SCANNER_STREAK"
    EDGE_SHADOW = "EDGE_SHADOW"

    # --- batch level --------------------------------------------------
    DUPLICATE_ID = "DUPLICATE_ID"
    DUPLICATE_ADJACENT = "DUPLICATE_ADJACENT"
    DUPLICATE_SEPARATED = "DUPLICATE_SEPARATED"
    DUPLICATE_DIFFERENT_SET = "DUPLICATE_DIFFERENT_SET"
    DUPLICATE_LOW_CONFIDENCE = "DUPLICATE_LOW_CONFIDENCE"

    # --- combinations and expectations --------------------------------
    MIXED_DEFECTS = "MIXED_DEFECTS"
    EXPECTED_FAILURE = "EXPECTED_FAILURE"


class CaseFamily(StrEnum):
    """A group of related test cases, switchable as one.

    Coarser than the tags on purpose: a user choosing a custom profile picks
    families, not sixty individual tags.
    """

    BASELINE = "baseline"
    STUDENT_ID = "student_id"
    SET_CODE = "set_code"
    ANSWERS = "answers"
    MARK_STYLES = "mark_styles"
    INTENSITY = "intensity"
    GEOMETRY = "geometry"
    CROPPING = "cropping"
    MARKERS = "markers"
    IMAGE_QUALITY = "image_quality"
    PAPER = "paper"
    DUPLICATES = "duplicates"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class MarkPlan:
    """What is drawn into one response group.

    Attributes:
        labels: The symbols marked, in *printed* order. Empty for a blank
            group; more than one for a deliberate double mark. Printed order
            matters: the engine reports ``"A-C"``, and a truth that said
            ``"C-A"`` would fail a correct reading.
        fill: Coverage of each mark, in ``[0, 1]``.
        style: How the marks were made.
        intensity: How dark they are, ``0``-``1``.
        offset_x: Horizontal displacement, in fractions of a bubble radius.
        offset_y: Vertical displacement.
        size_scale: Mark size multiplier.
        ambiguous: The mark is deliberately borderline - flagging it is
            correct behaviour, and the benchmark scores it that way.
        residue: The mark is erasure residue: too light to be an answer, and
            the expected reading of the group is *blank*.
    """

    labels: tuple[str, ...] = ()
    fill: float = STRONG_FILL
    style: MarkStyle = MarkStyle.FILL
    intensity: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    size_scale: float = 1.0
    ambiguous: bool = False
    residue: bool = False

    @property
    def effective_labels(self) -> tuple[str, ...]:
        """The labels a correct engine should report for this group.

        Erasure residue reports nothing: it is below the blank threshold, and
        a dataset that expected it to be read would be demanding the engine
        recover erased answers.
        """
        return () if self.residue else self.labels

    @property
    def value(self) -> str:
        """The ground-truth value this plan produces (``""``, ``"B"``, ``"B-D"``)."""
        return MULTIPLE_MARK_SEPARATOR.join(self.effective_labels)


@dataclass(frozen=True, slots=True)
class SheetCase:
    """One sheet's complete definition, before anything is drawn.

    Attributes:
        index: Position in the dataset, used for the file name.
        tags: What this sheet tests.
        marks: ``{zone id: {group key: plan}}`` - what to draw.
        answers: Expected value per question number, engine convention.
        ambiguous_questions: Questions whose mark is deliberately borderline.
        roll: Expected identifier string, engine convention (``"_"`` for a
            blank column, ``"?"`` for one that cannot resolve).
        roll_marks: What was actually drawn in each identifier column.
        roll_ambiguous: The identifier contains a borderline column, so
            either reading is acceptable.
        set_code: Expected set-code string.
        set_marks: What was actually drawn in each set-code column.
        set_ambiguous: As ``roll_ambiguous``, for the set code.
        distortion: Geometric and photometric degradation.
        omit_markers / faint_markers / damaged_markers: Registration marker
            damage, by canonical corner role.
        extra_marker: Draw an additional marker-shaped decoy.
        omit_orientation / faint_orientation: Orientation mark damage.
        expect_failure: This sheet should *not* register, and a benchmark must
            not count its refusal as a recognition error.
        duplicate_group: Identifier shared with other sheets in this dataset,
            or ``""``.
        notes: Free text for a human reading the ground truth.
    """

    index: int
    tags: tuple[TestCaseTag, ...]
    marks: dict[str, dict[int, MarkPlan]]
    answers: dict[int, str]
    ambiguous_questions: tuple[int, ...] = ()
    roll: str = ""
    roll_marks: tuple[tuple[str, ...], ...] = ()
    roll_ambiguous: bool = False
    set_code: str = ""
    set_marks: tuple[tuple[str, ...], ...] = ()
    set_ambiguous: bool = False
    distortion: DistortionSpec = field(default_factory=DistortionSpec)
    omit_markers: tuple[str, ...] = ()
    faint_markers: tuple[str, ...] = ()
    damaged_markers: tuple[str, ...] = ()
    extra_marker: bool = False
    omit_orientation: bool = False
    faint_orientation: bool = False
    expect_failure: bool = False
    duplicate_group: str = ""
    notes: str = ""

    @property
    def tag_values(self) -> tuple[str, ...]:
        """The tags as plain strings, for the ground-truth document."""
        return tuple(tag.value for tag in self.tags)


@dataclass(frozen=True, slots=True)
class FieldLayout:
    """What the selected template actually offers.

    Everything a case builder needs to know, read from the template once: how
    many identifier columns, which symbols, how many questions, which option
    labels. Nothing downstream may assume seven digits or four options - a
    template with a five-character alphanumeric code and six options is as
    valid, and the generator adapts rather than failing.

    Attributes:
        identifier_zone: Zone id of the candidate identifier, or ``None``.
        identifier_symbols: Symbols each identifier column offers.
        identifier_columns: How many printed columns it has.
        set_zone: Zone id of the set code, or ``None``.
        set_symbols: Symbols the set code offers.
        set_columns: How many printed positions it has.
        question_zones: ``(zone id, first question, labels, count)`` per block.
        questions: Every question number, ascending.
    """

    identifier_zone: str | None
    identifier_symbols: tuple[str, ...]
    identifier_columns: int
    set_zone: str | None
    set_symbols: tuple[str, ...]
    set_columns: int
    question_zones: tuple[tuple[str, int, tuple[str, ...], int], ...]
    questions: tuple[int, ...]

    @property
    def has_identifier(self) -> bool:
        """Whether this template has a candidate identifier field at all."""
        return self.identifier_zone is not None and self.identifier_columns > 0

    @property
    def has_set_code(self) -> bool:
        """Whether this template has a set-code field."""
        return self.set_zone is not None and self.set_columns > 0

    @property
    def has_questions(self) -> bool:
        """Whether this template has any questions."""
        return bool(self.questions)

    @property
    def option_labels(self) -> tuple[str, ...]:
        """The options the first question block offers."""
        return self.question_zones[0][2] if self.question_zones else ()

    def locate(self, question: int) -> tuple[str, int]:
        """Return ``(zone id, group key)`` for a question number.

        Raises:
            KeyError: No block declares that question.
        """
        for zone_id, first, _labels, count in self.question_zones:
            if first <= question < first + count:
                return zone_id, question - first
        raise KeyError(f"No question block contains question {question}")

    def labels_for(self, question: int) -> tuple[str, ...]:
        """Return the options one question offers."""
        for _zone_id, first, labels, count in self.question_zones:
            if first <= question < first + count:
                return labels
        return ()

    @classmethod
    def of(cls, template: OmrTemplate) -> FieldLayout:
        """Read the layout from a template.

        Optional fields are simply absent rather than fatal: a template with
        no set code should still produce a dataset, minus the set-code cases.
        """
        identifier_zone: str | None = None
        identifier_symbols: tuple[str, ...] = ()
        identifier_columns = 0
        set_zone: str | None = None
        set_symbols: tuple[str, ...] = ()
        set_columns = 0
        blocks: list[tuple[str, int, tuple[str, ...], int]] = []
        numbers: set[int] = set()

        for zone in template.zones:
            definition = zone.field
            if zone.grid is None or isinstance(definition, IgnoredFieldDefinition):
                continue
            if isinstance(definition, QuestionBlockFieldDefinition):
                labels = tuple(definition.answer_labels)
                blocks.append(
                    (zone.id, definition.first_question, labels, definition.question_count)
                )
                numbers.update(
                    range(
                        definition.first_question,
                        definition.first_question + definition.question_count,
                    )
                )
            elif isinstance(definition, GridFieldDefinition):
                kind = definition.type.value
                if kind in {"numeric", "candidate_id"} and identifier_zone is None:
                    identifier_zone = zone.id
                    identifier_symbols = tuple(definition.symbols)
                    identifier_columns = definition.character_count
                elif kind == "set_code" and set_zone is None:
                    set_zone = zone.id
                    set_symbols = tuple(definition.symbols)
                    set_columns = definition.character_count

        blocks.sort(key=lambda item: item[1])
        return cls(
            identifier_zone=identifier_zone,
            identifier_symbols=identifier_symbols,
            identifier_columns=identifier_columns,
            set_zone=set_zone,
            set_symbols=set_symbols,
            set_columns=set_columns,
            question_zones=tuple(blocks),
            questions=tuple(sorted(numbers)),
        )


def group_count(zone: Zone) -> int:
    """Return how many response groups a zone contains."""
    return len(list(zone_groups(zone)))


# ----------------------------------------------------------------------
# Building one sheet
# ----------------------------------------------------------------------
class SheetBuilder:
    """Accumulates one sheet's marks, and derives its truth from them.

    The single-source-of-truth rule lives here: a caller says "mark B on
    question 7" or "leave column 3 empty", and both the drawing instruction
    and the expected value come from that one statement.
    """

    def __init__(self, layout: FieldLayout, index: int, rng: random.Random) -> None:
        self.layout = layout
        self.index = index
        self.rng = rng
        self.marks: dict[str, dict[int, MarkPlan]] = {}
        self.tags: list[TestCaseTag] = []
        self._id_plans: dict[int, MarkPlan] = {}
        self._set_plans: dict[int, MarkPlan] = {}
        self._question_plans: dict[int, MarkPlan] = {}
        self.notes = ""

    # -- tagging -------------------------------------------------------
    def tag(self, *tags: TestCaseTag) -> SheetBuilder:
        """Record what this sheet is a test of."""
        for item in tags:
            if item not in self.tags:
                self.tags.append(item)
        return self

    # -- identifier ----------------------------------------------------
    def identifier(self, value: str, **kwargs: Any) -> SheetBuilder:
        """Mark the identifier columns to spell ``value``.

        A character that is not one of the field's symbols is skipped, leaving
        that column blank - which is how a shorter value than the field, or an
        alphanumeric field asked for a digit, degrades sensibly.
        """
        if not self.layout.has_identifier:
            return self
        for position, character in enumerate(value[: self.layout.identifier_columns]):
            if character in self.layout.identifier_symbols:
                self._id_plans[position] = MarkPlan(labels=(character,), **kwargs)
        return self

    def identifier_column(
        self, position: int, labels: Sequence[str], **kwargs: Any
    ) -> SheetBuilder:
        """Set one identifier column's marks explicitly (possibly several)."""
        if not self.layout.has_identifier:
            return self
        ordered = tuple(
            symbol for symbol in self.layout.identifier_symbols if symbol in set(labels)
        )
        self._id_plans[position] = MarkPlan(labels=ordered, **kwargs)
        return self

    def blank_identifier_column(self, position: int) -> SheetBuilder:
        """Leave one identifier column unmarked."""
        if self.layout.has_identifier:
            self._id_plans.pop(position, None)
            self._id_plans[position] = MarkPlan(labels=())
        return self

    # -- set code ------------------------------------------------------
    def set_code(self, value: str, **kwargs: Any) -> SheetBuilder:
        """Mark the set-code positions to spell ``value``."""
        if not self.layout.has_set_code:
            return self
        if self.layout.set_columns == 1 and value in self.layout.set_symbols:
            self._set_plans[0] = MarkPlan(labels=(value,), **kwargs)
            return self
        for position, character in enumerate(value[: self.layout.set_columns]):
            if character in self.layout.set_symbols:
                self._set_plans[position] = MarkPlan(labels=(character,), **kwargs)
        return self

    def set_column(self, position: int, labels: Sequence[str], **kwargs: Any) -> SheetBuilder:
        """Set one set-code position's marks explicitly."""
        if not self.layout.has_set_code:
            return self
        ordered = tuple(symbol for symbol in self.layout.set_symbols if symbol in set(labels))
        self._set_plans[position] = MarkPlan(labels=ordered, **kwargs)
        return self

    # -- questions -----------------------------------------------------
    def answer(self, question: int, labels: Sequence[str], **kwargs: Any) -> SheetBuilder:
        """Mark one question with the given options, in printed order."""
        available = self.layout.labels_for(question)
        ordered = tuple(label for label in available if label in set(labels))
        self._question_plans[question] = MarkPlan(labels=ordered, **kwargs)
        return self

    def blank(self, question: int) -> SheetBuilder:
        """Leave one question unanswered."""
        self._question_plans[question] = MarkPlan(labels=())
        return self

    def answer_all(self, **kwargs: Any) -> SheetBuilder:
        """Answer every question with a randomly chosen option."""
        for number in self.layout.questions:
            labels = self.layout.labels_for(number)
            if labels:
                self.answer(number, (self.rng.choice(list(labels)),), **kwargs)
        return self

    def blank_all(self) -> SheetBuilder:
        """Leave every question unanswered."""
        for number in self.layout.questions:
            self.blank(number)
        return self

    # -- finishing -----------------------------------------------------
    def build(self, **case_kwargs: Any) -> SheetCase:
        """Assemble the case, deriving every expected value from the marks."""
        marks: dict[str, dict[int, MarkPlan]] = {}
        if self.layout.identifier_zone and self._id_plans:
            marks[self.layout.identifier_zone] = dict(self._id_plans)
        if self.layout.set_zone and self._set_plans:
            marks[self.layout.set_zone] = dict(self._set_plans)

        answers: dict[int, str] = {}
        ambiguous: list[int] = []
        for number in self.layout.questions:
            plan = self._question_plans.get(number)
            if plan is None:
                plan = MarkPlan(labels=())
            zone_id, key = self.layout.locate(number)
            marks.setdefault(zone_id, {})[key] = plan
            answers[number] = plan.value
            if plan.ambiguous:
                ambiguous.append(number)

        roll, roll_marks, roll_ambiguous = _field_truth(
            self._id_plans, self.layout.identifier_columns
        )
        set_value, set_marks, set_ambiguous = _field_truth(
            self._set_plans, self.layout.set_columns
        )

        return SheetCase(
            index=self.index,
            tags=tuple(self.tags) or (TestCaseTag.BASELINE,),
            marks=marks,
            answers=answers,
            ambiguous_questions=tuple(sorted(ambiguous)),
            roll=roll,
            roll_marks=roll_marks,
            roll_ambiguous=roll_ambiguous,
            set_code=set_value,
            set_marks=set_marks,
            set_ambiguous=set_ambiguous,
            # A note passed here wins over one set on the builder, so a caller
            # can describe the finished case without having to clear the
            # builder's own note first.
            **{"notes": self.notes, **case_kwargs},
        )


def _field_truth(
    plans: dict[int, MarkPlan], columns: int
) -> tuple[str, tuple[tuple[str, ...], ...], bool]:
    """Derive a grid field's expected value from what was drawn.

    Returns ``(value, per-column marks, ambiguous)``, with the value written
    in the engine's own convention so a comparison is a string equality:
    the symbol when one bubble is marked, ``"_"`` when none is, and ``"?"``
    when the column cannot resolve to a single symbol.
    """
    if columns <= 0:
        return "", (), False

    characters: list[str] = []
    per_column: list[tuple[str, ...]] = []
    ambiguous = False

    for position in range(columns):
        plan = plans.get(position)
        labels = plan.effective_labels if plan is not None else ()
        per_column.append(labels)
        if plan is not None and plan.ambiguous:
            ambiguous = True
            characters.append(UNRESOLVED_CHARACTER)
        elif not labels:
            characters.append(BLANK_CHARACTER)
        elif len(labels) == 1:
            characters.append(labels[0])
        else:
            characters.append(UNRESOLVED_CHARACTER)

    return "".join(characters), tuple(per_column), ambiguous


__all__ = [
    "FAINT_FILL",
    "RESIDUE_FILL",
    "STRONG_FILL",
    "WEAK_FILL",
    "CaseFamily",
    "FieldLayout",
    "MarkPlan",
    "SheetBuilder",
    "SheetCase",
    "TestCaseTag",
    "group_count",
]
