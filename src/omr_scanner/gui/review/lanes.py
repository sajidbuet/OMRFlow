"""Turning a sheet's conflicts into the rectangles the reviewer sees.

Purpose:
    Answer one question for the Resolve stage's preview: *which parts of this
    page is a person responsible for, and what have they done about them?* -
    as plain geometry on the canonical page, so the overlay can draw it without
    knowing anything about conflicts and the page can test it without drawing.

Responsibilities:
    * :func:`build_lanes` - every highlighted response group on one sheet.
    * :func:`lane_for` - the rectangle one conflict occupies, or ``None`` when
      that conflict is not about a place on the paper at all.

What does NOT belong here:
    * Qt widgets, painting, colours or line styles. This module decides *what*
      is highlighted and *where*; :class:`~omr_scanner.gui.scan.preview.OverlayItem`
      decides how it looks.
    * Any judgement about a value. Which position is unresolved and which was
      decided comes from
      :func:`~omr_scanner.services.review_store.provenance_for`, and the
      geometry comes from the bubbles recognition measured. Nothing here reads
      a pixel or re-derives a decision.

Why the geometry comes from the bubbles and not from the template directly:
    The bubbles on a :class:`~omr_scanner.services.recognition_models.ScanResult`
    *are* the template's normalised geometry, projected onto the canonical page
    by the same transform recognition sampled through. Taking the rectangle
    from them means the highlight sits exactly where the engine looked - the
    one placement that cannot mislead a reviewer about what was measured - and
    that it follows a template edited afterwards without this module knowing
    the template was edited.

Why a *lane* rather than a bubble:
    A conflict is about a printed **position** - one column of a roll number -
    and the reviewer's question is "what is in this column", not "is this
    particular bubble filled". The preview used to answer the second question,
    with a ``?`` beside whichever bubble the engine nearly chose, which is
    where the machine's doubt landed rather than where the person's attention
    has to go. Every rectangle here spans the whole group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.review import ConflictState, FieldKind, ValueSource
from omr_scanner.gui.scan.preview import (
    LANE_PADDING_RATIO,
    FieldLane,
    LaneMark,
    LaneState,
)
from omr_scanner.services import group_cells

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.domain.review import Provenance
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import BubbleView, ConflictRecord, ScanResult

BLANK_NOTE = "BLANK"
"""The caption on a lane whose reviewer decided the position carries no mark.

A decision with no bubble to point at still has to look different from a
position nobody has touched, or "I checked, it is empty" and "nobody has
checked" become the same picture."""


def group_bubbles(
    result: ScanResult,
    template: OmrTemplate,
    conflict: ConflictRecord,
) -> tuple[BubbleView, ...]:
    """Return the bubbles of the response group one conflict is about.

    Args:
        result: The fresh reading of the sheet, whose bubbles carry the
            canonical-page geometry.
        template: The template it was read with, which defines the grouping.
        conflict: The conflict.

    Returns:
        The group's bubbles, or ``()`` when the conflict names no place on the
        paper - a corrupt image, a page that never registered.

    A conflict raised from the identifiers alone (a duplicate roll number)
    names no zone, because the pass that found it never saw one. The sheet has
    been re-read by the time it is reviewed, so the zone comes from the
    *engine's own* result rather than from a guess here.
    """
    zone_id = conflict.field.zone_id or _zone_for_kind(result, conflict.field.kind)
    if not zone_id:
        return ()
    if conflict.field.is_whole_field:
        return tuple(item for item in result.bubbles if item.zone_id == zone_id)
    cells = set(group_cells(template, zone_id, conflict.field.group_key))
    if not cells:
        return ()
    return tuple(
        item
        for item in result.bubbles
        if item.zone_id == zone_id and (item.row, item.column) in cells
    )


def _zone_for_kind(result: ScanResult, kind: FieldKind) -> str:
    """Return the zone a whole-field conflict refers to, when it names none."""
    if kind is FieldKind.IDENTIFIER:
        return result.identifier_zone_id or ""
    if kind is FieldKind.SET_CODE:
        return result.set_code_zone_id or ""
    return ""


NEIGHBOURING_GROUPS = 2
"""How many printed positions either side of the disputed one to keep in view.

A reviewer does not read an ambiguous roll-number column on its own; they read
it against the columns beside it, which the same candidate filled in the same
hand with the same pencil. Framing the disputed column alone removes the only
comparison available and makes a faint mark much harder to call.

Two rather than one because a position at the edge of a field then still has
two neighbours on the side that has them."""


def context_bubbles(
    result: ScanResult,
    template: OmrTemplate,
    conflict: ConflictRecord,
    *,
    neighbours: int = NEIGHBOURING_GROUPS,
) -> tuple[BubbleView, ...]:
    """Return the bubbles the zoomed view should frame for one conflict.

    Args:
        result: The fresh reading of the sheet.
        template: The template it was read with.
        conflict: The conflict being reviewed.
        neighbours: How many printed positions either side to include.

    Returns:
        The disputed group's bubbles plus its neighbours', or ``()`` when the
        conflict names no place on the paper.

    Deliberately wider than :func:`group_bubbles`, which is what gets
    *highlighted*. The highlight says "this position"; the framing says "and
    here is what it sits next to". Asking the template for the neighbouring
    groups rather than growing a rectangle by a guessed number of pixels means
    the context is a whole number of printed columns on any sheet design.

    A whole-field conflict already spans its field, so it gets no extra.
    """
    own = group_bubbles(result, template, conflict)
    if not own or conflict.field.is_whole_field or neighbours <= 0:
        return own

    zone_id = conflict.field.zone_id or _zone_for_kind(result, conflict.field.kind)
    wanted: set[tuple[int, int]] = set()
    for offset in range(-neighbours, neighbours + 1):
        key = conflict.field.group_key + offset
        if key < 0:
            continue
        wanted.update(group_cells(template, zone_id, key))
    if not wanted:
        return own
    return tuple(
        item
        for item in result.bubbles
        if item.zone_id == zone_id and (item.row, item.column) in wanted
    )


def bounds_of(bubbles: Sequence[BubbleView]) -> tuple[float, float, float, float]:
    """Return ``(left, top, right, bottom)`` of a set of bubbles."""
    return (
        min(item.x - item.width / 2.0 for item in bubbles),
        min(item.y - item.height / 2.0 for item in bubbles),
        max(item.x + item.width / 2.0 for item in bubbles),
        max(item.y + item.height / 2.0 for item in bubbles),
    )


def _machine_marks(bubbles: Sequence[BubbleView]) -> tuple[LaneMark, ...]:
    """Return the bubbles the engine read as marked in one group.

    Falls back to the group's darkest bubble when the engine selected none, so
    a position rejected as too faint still shows the reviewer *what* was nearly
    accepted. That is the whole question for an uncertain mark, and a lane with
    nothing ringed inside it would leave them hunting for it.
    """
    selected = [item for item in bubbles if item.selected]
    if not selected:
        selected = [item for item in bubbles if item.leading]
    return tuple(
        LaneMark(
            x=item.x, y=item.y, width=item.width, height=item.height, label=item.label
        )
        for item in selected
    )


def lane_for(
    result: ScanResult,
    template: OmrTemplate,
    conflict: ConflictRecord,
    found: Provenance | None,
    *,
    active: bool = False,
    pending: str | None = None,
) -> FieldLane | None:
    """Return the rectangle one conflict occupies, or ``None``.

    Args:
        result: The fresh reading of the sheet.
        template: The template it was read with.
        conflict: The conflict to draw.
        found: Where its current value came from, or ``None`` when that has not
            been read. Only this decides whether the lane is amber or red -
            never the conflict's cached state, which says a person acted but
            not what they decided.
        active: Whether this is the conflict being reviewed.
        pending: A value the reviewer has picked but not committed, or ``None``.
            ``""`` is a real pending choice - "this position is blank" - and is
            why this is not simply a falsy check.

    Returns:
        A lane, or ``None`` when the conflict corresponds to no single
        rectangle on the page.

    ``None`` is the honest answer for a sheet-scope conflict. "The page would
    not rectify" and "the lower-right corner was curled" are not positions, and
    forcing a rectangle onto the image for them would put a confident outline
    somewhere arbitrary. Those keep the displays they already have - the zone
    hatching for a scan-quality finding, and the message panel for the rest.
    """
    bubbles = group_bubbles(result, template, conflict)
    if not bubbles:
        return None

    left, top, right, bottom = bounds_of(bubbles)
    padding = (
        sum(item.width for item in bubbles) / len(bubbles)
    ) * LANE_PADDING_RATIO

    stored = found is not None and found.source is ValueSource.HUMAN
    # A pending choice wins the *display*, because it is what the reviewer is
    # looking at and about to commit. It never wins the record: nothing has
    # been written, and `LaneState.PENDING` is drawn so that it cannot be
    # mistaken for something that has.
    if pending is not None:
        value, state = pending, LaneState.PENDING
    elif stored and found is not None:
        value, state = found.value, LaneState.MANUAL
    else:
        value, state = "", LaneState.UNRESOLVED

    chosen = _chosen_bubble(bubbles, value) if state.is_manual else None
    return FieldLane(
        x=left - padding,
        y=top - padding,
        width=(right - left) + padding * 2.0,
        height=(bottom - top) + padding * 2.0,
        state=state,
        active=active,
        machine_marks=_machine_marks(bubbles),
        choice=chosen,
        note=_note_for(value, chosen) if state.is_manual else "",
    )


def _chosen_bubble(
    bubbles: Sequence[BubbleView], value: str
) -> LaneMark | None:
    """Return the bubble standing for a reviewer's decision, if one does.

    Matched on the **label** the template prints, not on a position in the
    stack: a set code whose symbols are ``"10"``, ``"11"``, ``"12"`` has three
    bubbles whose labels are two characters each, and indexing into the value
    would ring the wrong one while looking perfectly convincing.
    """
    if not value:
        return None
    found = next((item for item in bubbles if item.label == value), None)
    if found is None:
        return None
    return LaneMark(
        x=found.x,
        y=found.y,
        width=found.width,
        height=found.height,
        label=found.label,
    )


def _note_for(value: str, chosen: LaneMark | None) -> str:
    """Return the caption a manual lane carries, or ``""``.

    A lane whose chosen bubble is ringed needs no caption - the ring already
    says which value. A caption appears exactly where the ring cannot: a
    decision of "blank", and a value no single bubble on this sheet stands for
    (a whole roll number typed into the free-text box for a duplicate).
    """
    if chosen is not None:
        return ""
    return value or BLANK_NOTE


def build_lanes(
    result: ScanResult,
    template: OmrTemplate,
    conflicts: Sequence[ConflictRecord],
    provenance: Mapping[int, Provenance],
    *,
    active_conflict_id: int | None = None,
    pending: str | None = None,
) -> tuple[FieldLane, ...]:
    """Return every lane one sheet should show.

    Args:
        result: The fresh reading of the sheet.
        template: The template it was read with.
        conflicts: Every conflict on that sheet, whatever its state.
        provenance: Where each conflict's current value came from, keyed by
            conflict id. A conflict absent from it is drawn as unresolved.
        active_conflict_id: The conflict being reviewed, drawn more heavily.
        pending: A value picked but not yet committed on the active conflict.
            Applied to that lane only - an uncommitted choice belongs to the
            position the reviewer is looking at and nowhere else.

    Returns:
        The lanes, in the order the conflicts were given.

    **Every affected position, not only the selected one.** A roll number with
    three doubtful columns shows three lanes, so a reviewer can see how much of
    the field is in question before deciding any of it - and, once they have
    decided one, can see at a glance which of the others are left.

    A conflict the machine has withdrawn is drawn only if a person decided it:
    an outline round a position nobody disputes and nobody corrected would be
    ink with nothing behind it.
    """
    lanes: list[FieldLane] = []
    for conflict in conflicts:
        found = provenance.get(conflict.conflict_id)
        manual = found is not None and found.source is ValueSource.HUMAN
        if conflict.state is ConflictState.WITHDRAWN and not manual:
            continue
        active = conflict.conflict_id == active_conflict_id
        lane = lane_for(
            result,
            template,
            conflict,
            found,
            active=active,
            pending=pending if active else None,
        )
        if lane is not None:
            lanes.append(lane)
    return tuple(lanes)


__all__ = [
    "BLANK_NOTE",
    "NEIGHBOURING_GROUPS",
    "bounds_of",
    "build_lanes",
    "context_bubbles",
    "group_bubbles",
    "lane_for",
]
