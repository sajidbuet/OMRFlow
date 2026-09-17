"""Assembling per-bubble decisions into field values.

Purpose:
    Know which bubbles belong to the same response group, in which printed
    order, and what each group's symbol is - then hand each group to
    :func:`~omr_scanner.recognition.decide.decide_group` and collect the answers
    into a roll number, a set code or a list of question responses.

Responsibilities:
    * :func:`zone_groups` - the grid-index arithmetic that turns a zone's field
      definition into response groups. This is the one place the meaning of
      :class:`~omr_scanner.domain.template.SymbolAxis` is interpreted for
      recognition.
    * :func:`recognise_grid_zone` and :func:`recognise_question_zone` - the two
      zone kinds.
    * :func:`recognise_template` - every zone of one sheet.
    * :func:`choose_identifier_zone` / :func:`choose_set_code_zone` - which zone
      plays the roll-number and set-code roles.

What does NOT belong here:
    * Pixel access; measurements arrive already taken.
    * Deciding a single group, which is :mod:`omr_scanner.recognition.decide`.
    * Any assumption about how many symbols a set code has. A set code is one or
      more printed positions over whatever symbol list the template declares:
      ``A``/``B``/``C``/``D``, or two digit columns spelling ``"10"``, or
      ``"A1"``. Nothing here treats "one character" as a special case.

Axis convention:
    :class:`~omr_scanner.domain.template.SymbolAxis` says which way the
    *symbols* run, and the field's ``rows``/``columns`` properties already
    encode that. This module derives the cells of each group from the same rule,
    so a template that says "digits run down the page" and one that says "digits
    run across it" are read correctly without any per-template special casing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omr_scanner.domain.template import (
    FieldType,
    GridFieldDefinition,
    IgnoredFieldDefinition,
    QuestionBlockFieldDefinition,
    SymbolAxis,
)
from omr_scanner.recognition.decide import decide_group, reading_from_measurement
from omr_scanner.recognition.models import (
    FieldResult,
    FieldStatus,
    GroupDecision,
    MarkStatus,
    QuestionAnswer,
    SheetRecognition,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.domain.template import OmrTemplate, RecognitionSettings, Zone
    from omr_scanner.imaging.metrics import BubbleMeasurement

    MeasurementGrid = Mapping[tuple[int, int], BubbleMeasurement]
    """Measurements of one zone, keyed by ``(row, column)`` of its bubble grid."""

IDENTIFIER_KEYWORDS: tuple[str, ...] = ("roll", "student", "candidate", "registration", "id")
"""Words that mark a numeric zone as the candidate identifier.

Matched case-insensitively against a zone's id and label. A template with one
numeric field needs none of this - the single numeric zone is the identifier
either way; the keywords only break the tie when a sheet carries several numeric
fields (a roll number *and* a booklet serial, say), and the order zones happen
to appear in the document should not decide which one names the output file."""


@dataclass(frozen=True, slots=True)
class ZoneGroup:
    """One response group inside a zone.

    Attributes:
        key: The group's index within the zone - the character position for a
            grid field, the question *offset* (not its printed number) for a
            question block.
        cells: ``(row, column)`` of each bubble, in printed order.
        labels: The symbol each bubble stands for, in the same order.
    """

    key: int
    cells: tuple[tuple[int, int], ...]
    labels: tuple[str, ...]


def zone_groups(zone: Zone) -> tuple[ZoneGroup, ...]:
    """Return the response groups of ``zone``, in printed order.

    Args:
        zone: A template zone. Ignored zones have no groups.

    Returns:
        One :class:`ZoneGroup` per character position or per question.
    """
    field = zone.field
    if isinstance(field, IgnoredFieldDefinition):
        return ()

    if isinstance(field, GridFieldDefinition):
        symbols = field.symbols
        if field.symbol_axis is SymbolAxis.VERTICAL:
            # Each character position is a column; the symbols run down it.
            return tuple(
                ZoneGroup(
                    key=position,
                    cells=tuple((row, position) for row in range(len(symbols))),
                    labels=symbols,
                )
                for position in range(field.character_count)
            )
        # Each character position is a row; the symbols run across it.
        return tuple(
            ZoneGroup(
                key=position,
                cells=tuple((position, column) for column in range(len(symbols))),
                labels=symbols,
            )
            for position in range(field.character_count)
        )

    labels = field.answer_labels
    if field.symbol_axis is SymbolAxis.HORIZONTAL:
        # One row per question, the options running across it.
        return tuple(
            ZoneGroup(
                key=offset,
                cells=tuple((offset, column) for column in range(len(labels))),
                labels=labels,
            )
            for offset in range(field.question_count)
        )
    # One column per question, the options running down it.
    return tuple(
        ZoneGroup(
            key=offset,
            cells=tuple((row, offset) for row in range(len(labels))),
            labels=labels,
        )
        for offset in range(field.question_count)
    )


def _decide_zone_groups(
    zone: Zone, measurements: MeasurementGrid, *, settings: RecognitionSettings
) -> tuple[tuple[ZoneGroup, GroupDecision], ...]:
    """Decide every group of ``zone``; a cell with no measurement is unreadable."""
    decided: list[tuple[ZoneGroup, GroupDecision]] = []
    for group in zone_groups(zone):
        readings = tuple(
            reading_from_measurement(measurements.get(cell), label)
            for cell, label in zip(group.cells, group.labels, strict=True)
        )
        decided.append((group, decide_group(readings, settings=settings)))
    return tuple(decided)


def field_status(statuses: Sequence[MarkStatus]) -> FieldStatus:
    """Combine per-group outcomes into one field outcome.

    The order of the checks is the order of severity: anything that could not be
    read at all outranks a double mark, which outranks a faint one, which
    outranks a gap. A field is only ``RESOLVED`` when every position is.
    """
    if not statuses:
        return FieldStatus.BLANK
    if any(status is MarkStatus.UNREADABLE for status in statuses):
        return FieldStatus.UNREADABLE
    if any(status is MarkStatus.MULTIPLE for status in statuses):
        return FieldStatus.MULTIPLE
    if any(status is MarkStatus.UNCERTAIN for status in statuses):
        return FieldStatus.UNCERTAIN
    if all(status is MarkStatus.BLANK for status in statuses):
        return FieldStatus.BLANK
    if any(status is MarkStatus.BLANK for status in statuses):
        return FieldStatus.INCOMPLETE
    return FieldStatus.RESOLVED


def recognise_grid_zone(
    zone: Zone, measurements: MeasurementGrid, *, settings: RecognitionSettings
) -> FieldResult:
    """Recognise a numeric, alphanumeric or set-code zone.

    Args:
        zone: The zone to read.
        measurements: Its bubble measurements, keyed by ``(row, column)``.
        settings: The thresholds that apply to this zone.

    Returns:
        The assembled field value and the evidence for every position.

    Raises:
        ValueError: ``zone`` is not a character-grid zone.
    """
    if not isinstance(zone.field, GridFieldDefinition):
        raise ValueError(f"Zone '{zone.id}' is not a character-grid field")

    decided = _decide_zone_groups(zone, measurements, settings=settings)
    groups = tuple(decision for _, decision in decided)

    return FieldResult(
        zone_id=zone.id,
        label=zone.label,
        field_type=str(zone.field.type.value),
        value="".join(decision.character for decision in groups),
        status=field_status([decision.status for decision in groups]),
        groups=groups,
    )


def recognise_question_zone(
    zone: Zone, measurements: MeasurementGrid, *, settings: RecognitionSettings
) -> tuple[QuestionAnswer, ...]:
    """Recognise one question block.

    Args:
        zone: The zone to read.
        measurements: Its bubble measurements, keyed by ``(row, column)``.
        settings: The thresholds that apply to this zone.

    Returns:
        One answer per question, numbered as printed on the sheet.

    Raises:
        ValueError: ``zone`` is not a question block.
    """
    field = zone.field
    if not isinstance(field, QuestionBlockFieldDefinition):
        raise ValueError(f"Zone '{zone.id}' is not a question block")

    return tuple(
        QuestionAnswer(
            number=field.first_question + group.key,
            zone_id=zone.id,
            value=decision.value,
            status=decision.status,
            decision=decision,
        )
        for group, decision in _decide_zone_groups(zone, measurements, settings=settings)
    )


def choose_identifier_zone(template: OmrTemplate) -> Zone | None:
    """Return the zone that carries the candidate identifier (the roll number).

    A numeric zone whose id or label mentions one of
    :data:`IDENTIFIER_KEYWORDS` wins; failing that, the first numeric zone in
    template order. ``None`` when the template declares no numeric field, in
    which case nothing can be renamed after that identifier and the scan keeps a
    review file name.
    """
    numeric = [
        zone
        for zone in template.zones
        if isinstance(zone.field, GridFieldDefinition)
        and zone.field.type is FieldType.NUMERIC
    ]
    if not numeric:
        return None
    for zone in numeric:
        haystack = f"{zone.id} {zone.label}".lower()
        if any(keyword in haystack for keyword in IDENTIFIER_KEYWORDS):
            return zone
    return numeric[0]


def choose_set_code_zone(template: OmrTemplate) -> Zone | None:
    """Return the zone that carries the question-paper set code, or ``None``."""
    for zone in template.zones:
        field = zone.field
        if isinstance(field, GridFieldDefinition) and field.type is FieldType.SET_CODE:
            return zone
    return None


def recognise_template(
    template: OmrTemplate, measurements: Mapping[str, MeasurementGrid]
) -> SheetRecognition:
    """Recognise every readable zone of one sheet.

    Args:
        template: The template the sheet was aligned to.
        measurements: Bubble measurements per zone id, each keyed by
            ``(row, column)``. A zone missing from the mapping is read as
            entirely unreadable rather than skipped, so a field can never
            disappear silently from a result.

    Returns:
        The sheet's fields and answers, answers ordered by question number.
    """
    fields: list[FieldResult] = []
    answers: list[QuestionAnswer] = []

    for zone in template.zones:
        if isinstance(zone.field, IgnoredFieldDefinition):
            continue
        settings = template.effective_recognition(zone)
        grid = measurements.get(zone.id, {})
        if isinstance(zone.field, QuestionBlockFieldDefinition):
            answers.extend(recognise_question_zone(zone, grid, settings=settings))
        else:
            fields.append(recognise_grid_zone(zone, grid, settings=settings))

    identifier = choose_identifier_zone(template)
    set_code = choose_set_code_zone(template)
    return SheetRecognition(
        fields=tuple(fields),
        answers=tuple(sorted(answers, key=lambda answer: answer.number)),
        identifier_zone_id=identifier.id if identifier is not None else None,
        set_code_zone_id=set_code.id if set_code is not None else None,
    )


__all__ = [
    "IDENTIFIER_KEYWORDS",
    "ZoneGroup",
    "choose_identifier_zone",
    "choose_set_code_zone",
    "field_status",
    "recognise_grid_zone",
    "recognise_question_zone",
    "recognise_template",
    "zone_groups",
]
