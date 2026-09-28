"""Correcting a whole Student ID or set code on one sheet, as one operator action.

Purpose:
    The logic behind "type the whole field once": what the field reads now,
    what a typed value would change, which of those changes overrule a
    confident machine reading, and committing the result through the review
    ledger. Shared by every screen that offers the action - the Resolve stage
    and the Attendance stage - so that there is **one** definition of what a
    field correction is, and one path by which it is recorded.

Responsibilities:
    * :func:`validate_field_value` - typed text against the template's field.
    * :func:`current_field_values` - the field as it currently reads.
    * :func:`plan_field_edit` - which positions a value would change, and how.
    * :func:`commit_field_edit` - write it, through
      :func:`~omr_scanner.services.review_store.correct_field`.

What does NOT belong here:
    * Qt. A screen supplies the sheet's review records, their provenance and
      the freshly re-read result it already holds; this module decides and
      writes, and a screen decides what to draw.
    * A second correction mechanism. Everything is recorded by
      :func:`~omr_scanner.services.review_store.correct_field` - conflicts
      decided, confident readings overridden on
      :attr:`~omr_scanner.domain.review.ConflictType.MANUAL_OVERRIDE` records,
      every event audited and undoable as one group.

Why the machine's reading comes from a re-read result:
    A batch keeps no per-position evidence (see
    :mod:`omr_scanner.gui.review.worker`). The screen that shows the sheet has
    just re-read it with full evidence, and recognition is deterministic, so
    that re-read is exactly the reading the stored result came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omr_scanner.domain.review import ConflictState, FieldKind, MachineObservation
from omr_scanner.services.conflict_policy import join_field_value, split_field_value
from omr_scanner.services.review_store import correct_field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.review import Provenance, ReasonCode
    from omr_scanner.services.conflict_policy import FieldShape
    from omr_scanner.services.recognition_models import ScanResult
    from omr_scanner.services.review_store import ConflictRecord, FieldEdit

UNKNOWN_POSITION = "?"
"""What a field editor shows for a position it cannot state a value for.

A **display marker only.** It is the same character recognition uses in an
assembled identifier, and it is refused as *input*: an operator who leaves it
in the box is told to replace it rather than having it stored as somebody's
student ID."""

MAX_LISTED_SYMBOLS = 6
"""How many symbols a validation message names before it stops listing them.

"must contain exactly 6 digits" is more use to an operator than ten symbols
written out; a four-option set code is worth naming."""


@dataclass(frozen=True, slots=True)
class FieldEditPlan:
    """What one typed whole-field value would do, before any of it is written.

    Attributes:
        current: The field as it reads now, one entry per printed position.
        proposed: The value typed, split the same way.
        changes: ``conflict_id -> value`` for positions that already have a
            record - an unresolved conflict, or an earlier manual decision.
        overrides: ``position -> (machine reading, typed value)`` for positions
            the machine read **confidently** and nobody disputed. Writing these
            overrides the machine, which is why they are kept apart: they are
            the ones the operator is warned about.
        positions: Every position the edit changes, in printed order.
    """

    current: tuple[str, ...]
    proposed: tuple[str, ...]
    changes: dict[int, str]
    overrides: dict[int, tuple[str, str]]
    positions: tuple[int, ...]

    @property
    def is_empty(self) -> bool:
        """Whether applying this would change nothing."""
        return not self.changes and not self.overrides


def override_warning_text(field_label: str, overrides: dict[int, tuple[str, str]]) -> str:
    """The body of the "you are overriding a confident reading" confirmation.

    Pure, so a test can assert what an operator is told without a dialog.
    Positions are printed one-based, as the sheet numbers them.
    """
    count = len(overrides)
    noun = "value that was" if count == 1 else "values that were"
    rows = "\n".join(
        f"    Position {position + 1}     {machine or '(blank)'} → {typed or '(blank)'}"
        for position, (machine, typed) in sorted(overrides.items())
    )
    return (
        f"The entered {field_label} changes {count} {noun} read confidently:\n\n"
        f"{rows}\n\n"
        "These changes will override the machine result."
    )


def field_requirement(shape: FieldShape) -> str:
    """Say what a field will accept, in the template's own terms.

    Derived from the field definition rather than written out, so a project
    with a five-digit roll number or a two-position set code is told about
    *its* field. The alphabet is named only when it is short enough to read -
    "exactly 6 digits" is more use than ten symbols listed - and only when
    every position offers the same one.
    """
    positions = f"exactly {shape.length} position(s)"
    if not shape.is_uniform:
        return f"{shape.label} must contain {positions}, each a symbol that position prints."
    symbols = shape.positions[0]
    if set(symbols) <= set("0123456789"):
        kind = "digits"
    elif len(symbols) <= MAX_LISTED_SYMBOLS:
        kind = f"of {', '.join(symbols)}"
    else:
        kind = "symbols this field prints"
    return f"{shape.label} must contain {positions}, {kind}."


def validate_field_value(shape: FieldShape, text: str) -> tuple[list[str] | None, str]:
    """Check typed text against the template, and say why if it fails.

    Returns:
        ``(values, "")`` - one symbol per printed position - or
        ``(None, reason)``.

    Every rule comes from the field definition - how many positions, and which
    symbols each one prints. Nothing is padded, truncated or coerced: a value
    the field cannot hold is refused with a reason, because quietly turning it
    into one the field can hold would record an identifier nobody typed.
    """
    cleaned = text.strip()
    if not cleaned:
        return None, f"Enter the complete {shape.label}."
    if UNKNOWN_POSITION in cleaned:
        return None, (
            f"'{UNKNOWN_POSITION}' marks a position the machine could not read. "
            "Replace it with the value on the sheet."
        )
    values = split_field_value(shape, cleaned)
    if values is None:
        return None, field_requirement(shape)
    return values, ""


def machine_characters(result: ScanResult | None, shape: FieldShape) -> dict[int, str]:
    """What the engine read at each position of one field.

    From a freshly re-read result's
    :class:`~omr_scanner.services.recognition_models.CharacterView` per printed
    position. A reading the field cannot hold - a double mark ``"2-7"``, a
    blank - is shown as :data:`UNKNOWN_POSITION`.
    """
    if result is None:
        return {}
    found = next((item for item in result.fields if item.zone_id == shape.zone_id), None)
    if found is None:
        return {}
    return {
        character.position: character.value
        if shape.accepts(character.position, character.value)
        else UNKNOWN_POSITION
        for character in found.characters
    }


def _field_records(
    shape: FieldShape, records: Sequence[ConflictRecord]
) -> tuple[ConflictRecord | None, dict[int, ConflictRecord]]:
    """The field's live whole-field record, and its per-position records."""
    whole = next(
        (
            item
            for item in records
            if item.field.zone_id == shape.zone_id
            and item.field.is_whole_field
            and item.state is not ConflictState.WITHDRAWN
        ),
        None,
    )
    positions = {
        item.field.group_key: item
        for item in records
        if item.field.zone_id == shape.zone_id
        and not item.field.is_whole_field
        and item.state is not ConflictState.WITHDRAWN
    }
    return whole, positions


def current_field_values(
    shape: FieldShape,
    records: Sequence[ConflictRecord],
    provenance: Mapping[int, Provenance],
    machine: Mapping[int, str],
) -> list[str]:
    """The field as it currently reads, one entry per printed position.

    Args:
        shape: The field.
        records: The sheet's review records - every state, as the sheet shows
            them. Records of other fields are ignored.
        provenance: Where each record's value currently comes from.
        machine: :func:`machine_characters` for the same field.

    Built from the effective value of each position that has a record, and from
    the engine's own reading everywhere else. A field disputed *as a whole* and
    decided reads as that decision. A position nobody can currently put a value
    to contributes :data:`UNKNOWN_POSITION`.
    """
    whole, _ = _field_records(shape, records)
    if whole is not None:
        found = provenance.get(whole.conflict_id)
        if found is not None and found.is_human_decided:
            decided = split_field_value(shape, found.value)
            if decided is not None:
                return decided
    by_position = {
        item.field.group_key: item
        for item in records
        if item.field.zone_id == shape.zone_id and not item.field.is_whole_field
    }
    values: list[str] = []
    for position in range(shape.length):
        record = by_position.get(position)
        if record is None:
            values.append(machine.get(position, UNKNOWN_POSITION))
            continue
        found = provenance.get(record.conflict_id)
        value = found.value if found is not None else record.observation.value
        # A value the field cannot hold - a double mark - is not a reading the
        # editor may offer back as text.
        values.append(value if shape.accepts(position, value) else UNKNOWN_POSITION)
    return values


def plan_field_edit(
    shape: FieldShape,
    values: Sequence[str],
    records: Sequence[ConflictRecord],
    provenance: Mapping[int, Provenance],
    machine: Mapping[int, str],
) -> FieldEditPlan:
    """Decide which positions a typed value would change, and how.

    Three kinds of position can change, and the plan keeps them apart:

    * an **unresolved conflict** - the ordinary case;
    * a position with an **earlier manual decision**, which the new value
      supersedes;
    * a position the machine read **confidently**, with no record at all. The
      explicit field editor is the operator saying "I can see the whole
      number", so it may overrule such a reading - a clean ``9`` that is an
      ``8`` on the paper - but only as a named override, after a warning, and
      never as a disguised conflict. These are :attr:`FieldEditPlan.overrides`.

    A field disputed as a whole (a wholly blank Student ID) is settled as that
    one record rather than as per-position overrides of a reading nobody made.
    Positions the machine already reads correctly are left alone.
    """
    current = current_field_values(shape, records, provenance, machine)
    whole, by_position = _field_records(shape, records)
    if whole is not None:
        proposed = join_field_value(values)
        found = provenance.get(whole.conflict_id)
        settled = found is not None and found.is_human_decided and found.value == proposed
        return FieldEditPlan(
            current=tuple(current),
            proposed=tuple(values),
            changes={} if settled else {whole.conflict_id: proposed},
            overrides={},
            positions=tuple(
                index
                for index, (before, after) in enumerate(zip(current, values, strict=True))
                if before != after
            ),
        )
    changes: dict[int, str] = {}
    overrides: dict[int, tuple[str, str]] = {}
    positions: list[int] = []
    for position, wanted in enumerate(values):
        record = by_position.get(position)
        if record is None:
            if current[position] != wanted:
                overrides[position] = (current[position], wanted)
                positions.append(position)
            continue
        found = provenance.get(record.conflict_id)
        if found is not None and found.is_human_decided and found.value == wanted:
            continue
        changes[record.conflict_id] = wanted
        positions.append(position)
    return FieldEditPlan(
        current=tuple(current),
        proposed=tuple(values),
        changes=changes,
        overrides=overrides,
        positions=tuple(positions),
    )


def machine_observation_at(
    result: ScanResult | None, shape: FieldShape, position: int
) -> MachineObservation:
    """What the engine read at one position, as an override record keeps it.

    Taken verbatim from the re-read result's
    :class:`~omr_scanner.services.recognition_models.CharacterView`, so an
    override stores exactly what it overruled.
    """
    fields = result.fields if result is not None else ()
    found = next((item for item in fields if item.zone_id == shape.zone_id), None)
    character = next(
        (
            item
            for item in (found.characters if found is not None else ())
            if item.position == position
        ),
        None,
    )
    if character is None:
        return MachineObservation(value=machine_characters(result, shape).get(position, ""))
    return MachineObservation(
        value=character.value,
        status=character.status,
        confidence=character.confidence,
        top_fill=character.top_fill,
        margin=character.margin,
        detail="Read confidently; recognition raised no conflict here.",
    )


def commit_field_edit(
    database: ProjectDatabase,
    *,
    batch_id: str,
    scan_id: int,
    shape: FieldShape,
    kind: FieldKind,
    plan: FieldEditPlan,
    records: Sequence[ConflictRecord],
    result: ScanResult | None,
    reviewer: str,
    reason: ReasonCode,
    reason_text: str = "",
    context: str = "",
) -> FieldEdit:
    """Record a planned field edit through the review ledger. One transaction.

    Args:
        database: The open project database.
        batch_id: The batch the sheet belongs to.
        scan_id: The sheet.
        shape: The field edited.
        kind: Which identity field it is - recorded on any override record, so
            it is never taken from whichever record happened to be selected.
        plan: :func:`plan_field_edit`'s answer, already confirmed by the
            operator when it overrides anything.
        records: The same review records the plan was made from.
        result: The re-read result the machine observations come from.
        reviewer: Who decided. Required.
        reason: Why. Shared by every position.
        reason_text: The operator's note.
        context: Where the edit was made, appended to every event's detail -
            "made from the Attendance stage while investigating ..." - so the
            ledger says *why* the operator was looking at this sheet.

    Returns:
        What :func:`~omr_scanner.services.review_store.correct_field` wrote.

    Raises:
        ReviewError: No reviewer, a missing explanation, or nothing to write.
    """
    group_of_record = {item.conflict_id: item.field.group_key for item in records}
    positions = {
        group_of_record[conflict_id]: value
        for conflict_id, value in plan.changes.items()
        if conflict_id in group_of_record
    }
    positions.update(
        {position: typed for position, (_machine, typed) in plan.overrides.items()}
    )
    return correct_field(
        database,
        batch_id=batch_id,
        scan_id=scan_id,
        zone_id=shape.zone_id,
        values=positions,
        display_value=join_field_value(plan.proposed),
        field_label=shape.label,
        reviewer=reviewer,
        reason=reason,
        reason_text=reason_text,
        overrides={
            position: machine_observation_at(result, shape, position)
            for position in plan.overrides
        },
        field_kind=kind,
        previous_value=join_field_value(plan.current),
        context=context,
    )


__all__ = [
    "MAX_LISTED_SYMBOLS",
    "UNKNOWN_POSITION",
    "FieldEditPlan",
    "commit_field_edit",
    "current_field_values",
    "field_requirement",
    "machine_characters",
    "machine_observation_at",
    "override_warning_text",
    "plan_field_edit",
    "validate_field_value",
]
