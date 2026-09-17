"""The vocabulary of a recognition result.

Purpose:
    Give every stage of interpretation - one bubble, one response group, one
    field, one sheet - an explicit type, so that a result travels as a named
    structure rather than as nested dictionaries that only the producing
    function understands.

Responsibilities:
    * Name the outcomes a response group can have (:class:`MarkStatus`) and the
      outcomes a whole field can have (:class:`FieldStatus`).
    * Carry the measurements behind a decision, never only the decision, so that
      Phase 6's conflict queue can show a human *why* something was flagged.

What does NOT belong here:
    * Thresholds. Those come from
      :class:`~omr_scanner.domain.template.RecognitionSettings`, which lives in
      the template because a threshold is only meaningful for the sheet design
      and print quality it was tuned against.
    * Pixel access. These types describe measurements that
      :mod:`omr_scanner.imaging.metrics` already produced.

The multiple-mark convention:
    A group with two marks keeps **both**. Its value is the selected labels
    joined by :data:`MULTIPLE_MARK_SEPARATOR` (``"B-D"``), and its status is
    :attr:`MarkStatus.MULTIPLE`. Nothing in this package ever picks the darker
    of two marks and reports it as the answer: that is precisely the decision a
    human has to make, and discarding the competing mark would hide the
    question from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MULTIPLE_MARK_SEPARATOR = "-"
"""Separator between the labels of a multiply-marked group (``"B-D"``)."""

BLANK_VALUE = ""
"""How "no mark at all" is represented in a value string.

The empty string rather than a sentinel like ``"BLANK"`` so that an exported
CSV cell is simply empty, which is what both a spreadsheet and a human reader
expect, and so that concatenating character values cannot accidentally produce
a plausible-looking identifier."""

UNRESOLVED_CHARACTER = "?"
"""Placeholder for one character position that could not be read confidently.

A roll number with an unreadable third digit renders as ``"21?3123"``: visibly
wrong, impossible to mistake for a real identifier, and never used as a file
name (see :mod:`omr_scanner.services.filename_manager`)."""

BLANK_CHARACTER = "_"
"""Placeholder for one character position that carries no mark at all.

Distinct from :data:`UNRESOLVED_CHARACTER` because the two need different human
responses: a blank column is usually a candidate who left a digit out, while an
unreadable one is usually a scan or printing problem."""


class MarkStatus(StrEnum):
    """Outcome of interpreting one response group.

    A *response group* is one set of mutually exclusive bubbles: the ten digits
    of one roll-number column, or the four options of one question.
    """

    RESOLVED = "resolved"
    """Exactly one mark, clearly ahead of the next darkest bubble."""

    BLANK = "blank"
    """No bubble reached even the "certainly empty" threshold."""

    MULTIPLE = "multiple"
    """More than one bubble crossed the fill threshold. Both are kept."""

    UNCERTAIN = "uncertain"
    """A mark too faint to accept, or too close to its runner-up to separate."""

    UNREADABLE = "unreadable"
    """The bubbles could not be sampled - the group fell outside the page."""


class FieldStatus(StrEnum):
    """Outcome of interpreting one whole field (a zone's worth of groups)."""

    RESOLVED = "resolved"
    """Every character position was resolved."""

    BLANK = "blank"
    """Every character position was blank - the candidate left the field empty."""

    INCOMPLETE = "incomplete"
    """Some positions resolved and some blank; the value has gaps."""

    MULTIPLE = "multiple"
    """At least one position carried more than one mark."""

    UNCERTAIN = "uncertain"
    """At least one position was too faint or too close to call."""

    UNREADABLE = "unreadable"
    """At least one position could not be sampled at all."""


@dataclass(frozen=True, slots=True)
class BubbleReading:
    """One bubble, as the decision layer sees it.

    Attributes:
        label: The symbol this bubble stands for (``"7"``, ``"B"``).
        fill_ratio: Fraction of the sampled interior classified as ink.
        mean_darkness: ``1 - mean(sample)/255`` over the same sample.
        contrast: How much darker the interior is than the paper beside it.
        usable: Whether the sample could be taken at all.
    """

    label: str
    fill_ratio: float
    mean_darkness: float
    contrast: float
    usable: bool


@dataclass(frozen=True, slots=True)
class GroupDecision:
    """What one response group was decided to contain, and on what evidence.

    Attributes:
        readings: Every bubble in the group, in printed order.
        selected: Indices into :attr:`readings` of the bubbles that crossed the
            fill threshold. Empty for a blank or too-faint group; more than one
            for a multiply-marked group, which is never reduced to a single
            answer here.
        status: The outcome.
        leading_index: Index of the darkest bubble, whether or not it was
            selected. ``None`` when nothing was usable. Lets a reviewer see what
            the sheet *nearly* said without that guess becoming the answer.
        top_fill: Highest fill ratio in the group.
        runner_up_fill: Second highest fill ratio; ``0.0`` for a single-bubble
            group.
        margin: ``top_fill - runner_up_fill``. The quantity that actually
            decides whether one mark stands out, as opposed to its absolute
            darkness, which varies with pencil, pressure and scanner.
        confidence: A bounded, interpretable measure in ``[0, 1]`` - see
            :func:`~omr_scanner.recognition.decide.decide_group` for the exact
            definition per status. Never a fabricated percentage: a group that
            needs a human is reported as ``0.0`` rather than as "40% sure".
        needs_review: Whether this group should be queued for human resolution.
    """

    readings: tuple[BubbleReading, ...]
    selected: tuple[int, ...]
    status: MarkStatus
    leading_index: int | None
    top_fill: float
    runner_up_fill: float
    margin: float
    confidence: float
    needs_review: bool

    @property
    def value(self) -> str:
        """The group's value: ``""``, ``"B"``, or ``"B-D"`` for a double mark."""
        if not self.selected:
            return BLANK_VALUE
        return MULTIPLE_MARK_SEPARATOR.join(self.readings[i].label for i in self.selected)

    @property
    def leading_label(self) -> str:
        """Label of the darkest bubble, selected or not; ``""`` when unusable."""
        if self.leading_index is None:
            return BLANK_VALUE
        return self.readings[self.leading_index].label

    @property
    def character(self) -> str:
        """This group rendered as exactly one character of a field value.

        Resolved groups contribute their symbol, blank ones
        :data:`BLANK_CHARACTER`, and everything else
        :data:`UNRESOLVED_CHARACTER` - so a field value has one character per
        printed column and stays readable as an identifier.

        A multi-character symbol (a set code whose options are ``"10"``,
        ``"11"``, ``"12"``) contributes the whole symbol; "one character" is the
        printed *position*, not a single letter.
        """
        if self.status is MarkStatus.RESOLVED:
            return self.readings[self.selected[0]].label
        if self.status is MarkStatus.BLANK:
            return BLANK_CHARACTER
        return UNRESOLVED_CHARACTER


@dataclass(frozen=True, slots=True)
class FieldResult:
    """One recognised zone: a roll number, a set code, a custom field.

    Attributes:
        zone_id: The template zone this came from.
        label: The zone's human readable name.
        field_type: The zone's field type, as the string value of
            :class:`~omr_scanner.domain.template.FieldType`.
        value: The assembled value, one entry per printed character position.
            Unresolved positions appear as :data:`UNRESOLVED_CHARACTER` and
            blank ones as :data:`BLANK_CHARACTER`, so the string is never a
            plausible-but-wrong identifier.
        status: The field's overall outcome.
        groups: Per-position decisions, in printed order.
    """

    zone_id: str
    label: str
    field_type: str
    value: str
    status: FieldStatus
    groups: tuple[GroupDecision, ...]

    @property
    def is_reliable(self) -> bool:
        """Whether every position resolved - the bar for using this as a file name."""
        return self.status is FieldStatus.RESOLVED

    @property
    def needs_review(self) -> bool:
        """Whether any position should be shown to a human."""
        return any(group.needs_review for group in self.groups)


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    """One question's recognised response.

    Attributes:
        number: The question number printed on the sheet (1-based).
        zone_id: The question-block zone the question belongs to.
        value: ``""`` when blank, ``"B"`` when single, ``"B-D"`` when multiple.
        status: The outcome.
        decision: The full evidence behind it.
    """

    number: int
    zone_id: str
    value: str
    status: MarkStatus
    decision: GroupDecision

    @property
    def needs_review(self) -> bool:
        """Whether this answer should be queued for human resolution."""
        return self.decision.needs_review


@dataclass(frozen=True, slots=True)
class SheetRecognition:
    """Everything recognised from one rectified sheet.

    Attributes:
        fields: Non-question zones, in template order.
        answers: Question responses, ordered by question number.
        identifier_zone_id: Zone chosen as the candidate identifier (the roll
            number), or ``None`` when the template defines no numeric field.
        set_code_zone_id: Zone chosen as the question-paper set code, or
            ``None``.
    """

    fields: tuple[FieldResult, ...]
    answers: tuple[QuestionAnswer, ...]
    identifier_zone_id: str | None = None
    set_code_zone_id: str | None = None

    def field(self, zone_id: str | None) -> FieldResult | None:
        """Return the field for ``zone_id``, or ``None``."""
        if zone_id is None:
            return None
        return next((item for item in self.fields if item.zone_id == zone_id), None)

    @property
    def identifier(self) -> FieldResult | None:
        """The candidate identifier field (roll number), when the template has one."""
        return self.field(self.identifier_zone_id)

    @property
    def set_code(self) -> FieldResult | None:
        """The question-paper set-code field, when the template has one."""
        return self.field(self.set_code_zone_id)

    @property
    def review_count(self) -> int:
        """How many fields and answers need a human decision."""
        return sum(item.needs_review for item in self.fields) + sum(
            answer.needs_review for answer in self.answers
        )

    @property
    def multiple_mark_count(self) -> int:
        """How many questions carry more than one mark."""
        return sum(answer.status is MarkStatus.MULTIPLE for answer in self.answers)

    @property
    def blank_answer_count(self) -> int:
        """How many questions carry no mark at all."""
        return sum(answer.status is MarkStatus.BLANK for answer in self.answers)


__all__ = [
    "BLANK_CHARACTER",
    "BLANK_VALUE",
    "MULTIPLE_MARK_SEPARATOR",
    "UNRESOLVED_CHARACTER",
    "BubbleReading",
    "FieldResult",
    "FieldStatus",
    "GroupDecision",
    "MarkStatus",
    "QuestionAnswer",
    "SheetRecognition",
]
