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
from omr_scanner.gui.scan.preview import LANE_PADDING_RATIO, FieldLane, LaneState
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


def lane_for(
    result: ScanResult,
    template: OmrTemplate,
    conflict: ConflictRecord,
    found: Provenance | None,
    *,
    active: bool = False,
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

    left = min(item.x - item.width / 2.0 for item in bubbles)
    right = max(item.x + item.width / 2.0 for item in bubbles)
    top = min(item.y - item.height / 2.0 for item in bubbles)
    bottom = max(item.y + item.height / 2.0 for item in bubbles)
    padding = (
        sum(item.width for item in bubbles) / len(bubbles)
    ) * LANE_PADDING_RATIO

    manual = found is not None and found.source is ValueSource.HUMAN
    chosen = _chosen_bubble(bubbles, found) if manual else None
    return FieldLane(
        x=left - padding,
        y=top - padding,
        width=(right - left) + padding * 2.0,
        height=(bottom - top) + padding * 2.0,
        state=LaneState.MANUAL if manual else LaneState.UNRESOLVED,
        active=active,
        choice_x=chosen.x if chosen is not None else 0.0,
        choice_y=chosen.y if chosen is not None else 0.0,
        choice_width=chosen.width if chosen is not None else 0.0,
        choice_height=chosen.height if chosen is not None else 0.0,
        note=_note_for(found, chosen) if manual else "",
    )


def _chosen_bubble(
    bubbles: Sequence[BubbleView], found: Provenance | None
) -> BubbleView | None:
    """Return the bubble standing for a reviewer's decision, if one does.

    Matched on the **label** the template prints, not on a position in the
    stack: a set code whose symbols are ``"10"``, ``"11"``, ``"12"`` has three
    bubbles whose labels are two characters each, and indexing into the value
    would ring the wrong one while looking perfectly convincing.
    """
    if found is None or not found.value:
        return None
    return next((item for item in bubbles if item.label == found.value), None)


def _note_for(found: Provenance | None, chosen: BubbleView | None) -> str:
    """Return the caption a manual lane carries, or ``""``.

    A lane whose chosen bubble is ringed needs no caption - the ring already
    says which value. A caption appears exactly where the ring cannot: a
    decision of "blank", and a value no single bubble on this sheet stands for
    (a whole roll number typed into the free-text box for a duplicate).
    """
    if chosen is not None:
        return ""
    if found is None:
        return ""
    return found.value or BLANK_NOTE


def build_lanes(
    result: ScanResult,
    template: OmrTemplate,
    conflicts: Sequence[ConflictRecord],
    provenance: Mapping[int, Provenance],
    *,
    active_conflict_id: int | None = None,
) -> tuple[FieldLane, ...]:
    """Return every lane one sheet should show.

    Args:
        result: The fresh reading of the sheet.
        template: The template it was read with.
        conflicts: Every conflict on that sheet, whatever its state.
        provenance: Where each conflict's current value came from, keyed by
            conflict id. A conflict absent from it is drawn as unresolved.
        active_conflict_id: The conflict being reviewed, drawn more heavily.

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
        lane = lane_for(
            result,
            template,
            conflict,
            found,
            active=conflict.conflict_id == active_conflict_id,
        )
        if lane is not None:
            lanes.append(lane)
    return tuple(lanes)


__all__ = ["BLANK_NOTE", "build_lanes", "group_bubbles", "lane_for"]
