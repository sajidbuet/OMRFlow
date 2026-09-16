"""The designer's in-progress document: template, history and marker provenance.

Purpose:
    Hold everything one editing session needs that is *not* a widget: the
    current (immutable) template, its undo/redo stack, where it will be saved,
    and per-marker "how did this geometry get here" status that is useful
    while editing but is never written to the ``.omrt`` file (see
    ``docs/phase_02_plan.md`` §2 for why).

Responsibilities:
    * Own the single :class:`~omr_scanner.gui.template_designer.history.SnapshotHistory`
      for the session and expose the mutations the designer performs
      (add/update/remove/duplicate/move/resize a zone; set a marker) as methods
      that each push exactly one snapshot.
    * Track whether the document has unsaved changes.

What does NOT belong here:
    * Any Qt import. This class is exercised directly in unit tests with no
      `QApplication`, which is what makes the mutation logic trustworthy
      independently of whether the canvas wires it up correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import (
    BubbleGrid,
    BubbleOverride,
    MarkerRole,
    OrientationMarker,
    RegistrationMarker,
    Zone,
)
from omr_scanner.domain.template_authoring import resize_zone, translate_zone
from omr_scanner.gui.template_designer.history import SnapshotHistory

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from omr_scanner.domain.geometry import NormalizedRect
    from omr_scanner.domain.template import OmrTemplate


class DetectionMethod(StrEnum):
    """How a marker's current geometry was determined."""

    AUTO = "auto"
    """Placed by :mod:`omr_scanner.services.marker_detection_service`."""

    MANUAL = "manual"
    """Placed or moved by the user - including every marker loaded from a
    saved template, since by the time it was saved a user had already
    accepted it."""


@dataclass(frozen=True, slots=True)
class MarkerStatus:
    """Session-only provenance for one registration or orientation marker.

    Never persisted (see the module docstring); rebuilt each session from
    whatever the last detection or edit produced.

    Attributes:
        confirmed: Whether the user has explicitly accepted this geometry.
            An auto-detected marker starts unconfirmed so the canvas can draw
            it distinctly until the user looks at it.
        method: How the current geometry was produced.
        confidence: The detector's score in ``[0, 1]``, or ``None`` for a
            manually placed marker (a person's placement has no "confidence").
    """

    confirmed: bool
    method: DetectionMethod
    confidence: float | None = None


MANUAL_CONFIRMED = MarkerStatus(confirmed=True, method=DetectionMethod.MANUAL)
"""The status every marker starts with when loaded from a saved template."""

MISSING = MarkerStatus(confirmed=False, method=DetectionMethod.MANUAL, confidence=None)
"""The status for a corner nothing has been placed at yet."""


class DesignerState:
    """Mutable session state wrapping an immutable template document.

    Args:
        template: The template being edited.
        template_path: Where it will be saved; ``None`` for a template that
            has never been saved (the title bar shows "Untitled").
        reference_image_path: The reference sheet image on disk, if any.
    """

    def __init__(
        self,
        template: OmrTemplate,
        *,
        template_path: Path | None = None,
        reference_image_path: Path | None = None,
    ) -> None:
        self.history: SnapshotHistory[OmrTemplate] = SnapshotHistory(template)
        self.template_path = template_path
        self.reference_image_path = reference_image_path
        self._clean_template = template
        self.marker_status: dict[MarkerRole, MarkerStatus] = dict.fromkeys(
            MarkerRole, MANUAL_CONFIRMED
        )
        self.orientation_status: MarkerStatus = MANUAL_CONFIRMED

    # ------------------------------------------------------------------
    # Document state
    # ------------------------------------------------------------------
    @property
    def template(self) -> OmrTemplate:
        """The document as it stands right now."""
        return self.history.current

    @property
    def is_dirty(self) -> bool:
        """Whether the document differs from what was last saved (or loaded)."""
        return self.template != self._clean_template

    def mark_saved(self, path: Path) -> None:
        """Record that the current document was written to ``path``."""
        self.template_path = path
        self._clean_template = self.template

    def load(
        self, template: OmrTemplate, *, template_path: Path, reference_image_path: Path | None
    ) -> None:
        """Replace the document entirely (File > Open), discarding history.

        The previous document's undo stack describes edits to a *different*
        template and would be meaningless applied to this one.
        """
        self.history.reset(template)
        self.template_path = template_path
        self.reference_image_path = reference_image_path
        self._clean_template = template
        self.marker_status = dict.fromkeys(MarkerRole, MANUAL_CONFIRMED)
        self.orientation_status = MANUAL_CONFIRMED

    def undo(self) -> OmrTemplate:
        """Move to the previous document state. See :meth:`SnapshotHistory.undo`."""
        return self.history.undo()

    def redo(self) -> OmrTemplate:
        """Move to the next document state. See :meth:`SnapshotHistory.redo`."""
        return self.history.redo()

    def _apply(self, updated: OmrTemplate) -> None:
        """Push one completed edit."""
        self.history.push(updated)

    def apply_template(self, updated: OmrTemplate) -> None:
        """Replace the whole document in one history entry.

        The most general primitive here, for an edit that changes a
        template-level setting *and* the zones that follow from it in a single
        user gesture - changing the default bubble radius, which resizes every
        inheriting region. Pushing the setting and each zone separately would let
        one Undo leave the document half-converted.

        Unlike :meth:`load`, history is kept: this is an edit to the *same*
        document, not a different one.
        """
        self._apply(updated)

    # ------------------------------------------------------------------
    # Registration and orientation markers
    # ------------------------------------------------------------------
    def set_marker(
        self, role: MarkerRole, marker: RegistrationMarker, *, status: MarkerStatus
    ) -> None:
        """Replace the marker for ``role`` and record how it got there."""
        updated = tuple(
            marker if existing.role is role else existing
            for existing in self.template.registration_markers
        )
        self._apply(self.template.model_copy(update={"registration_markers": updated}))
        self.marker_status = {**self.marker_status, role: status}

    def confirm_marker(self, role: MarkerRole) -> None:
        """Mark a detected-but-unconfirmed marker as accepted, with no geometry change."""
        current = self.marker_status[role]
        self.marker_status = {**self.marker_status, role: replace(current, confirmed=True)}

    def set_orientation_marker(self, marker: OrientationMarker, *, status: MarkerStatus) -> None:
        """Replace the orientation marker and record how it got there."""
        self._apply(self.template.model_copy(update={"orientation_marker": marker}))
        self.orientation_status = status

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------
    def apply_zones(self, zones: Sequence[Zone]) -> None:
        """Replace the entire zones tuple in one history entry.

        The batch primitive every other zone mutation below is a thin,
        single-purpose wrapper around. Used directly when one user gesture
        must touch multiple zones at once - Create Column Array (which both
        re-tags the reference zone with a new ``group_id`` and appends its
        generated siblings) and Distribute Columns Evenly (which moves every
        intermediate column) - where pushing one history entry per touched
        zone would let a single Undo only partially reverse the gesture.
        """
        self._apply(self.template.model_copy(update={"zones": tuple(zones)}))

    def add_zones(self, zones: Sequence[Zone]) -> None:
        """Append one or more new zones (a region generator may produce several)."""
        self.apply_zones((*self.template.zones, *zones))

    def replace_zone(self, zone_id: str, updated: Zone) -> None:
        """Replace the zone identified by ``zone_id`` with ``updated``.

        Raises:
            KeyError: No zone has that id.
        """
        if self.template.zone_by_id(zone_id) is None:
            raise KeyError(f"No zone with id '{zone_id}'")
        self.apply_zones(
            tuple(updated if zone.id == zone_id else zone for zone in self.template.zones)
        )

    def remove_zone(self, zone_id: str) -> None:
        """Delete the zone identified by ``zone_id``, if present."""
        self.apply_zones(tuple(zone for zone in self.template.zones if zone.id != zone_id))

    def move_zone(self, zone_id: str, *, dx: float, dy: float) -> None:
        """Shift a zone by a normalised offset, clamped to the page."""
        zone = self.template.zone_by_id(zone_id)
        if zone is None:
            raise KeyError(f"No zone with id '{zone_id}'")
        self.replace_zone(zone_id, translate_zone(zone, dx=dx, dy=dy))

    def resize_zone(self, zone_id: str, *, bounds: NormalizedRect) -> None:
        """Change a zone's bounds, re-fitting its bubble grid to the new size."""
        zone = self.template.zone_by_id(zone_id)
        if zone is None:
            raise KeyError(f"No zone with id '{zone_id}'")
        self.replace_zone(zone_id, resize_zone(zone, bounds=bounds))

    def duplicate_zone(
        self, zone_id: str, *, new_id: str, dx: float = 0.03, dy: float = 0.03
    ) -> Zone:
        """Copy a zone under a new id, offset slightly so it does not sit exactly on top.

        Args:
            zone_id: The zone to copy.
            new_id: Id for the copy; must not already exist in the template.
            dx: Horizontal offset applied to the copy, normalised.
            dy: Vertical offset applied to the copy, normalised.

        Returns:
            The newly added zone.

        Raises:
            KeyError: ``zone_id`` does not exist.
            ValueError: ``new_id`` is already used by another zone.
        """
        original = self.template.zone_by_id(zone_id)
        if original is None:
            raise KeyError(f"No zone with id '{zone_id}'")
        if self.template.zone_by_id(new_id) is not None:
            raise ValueError(f"Zone id '{new_id}' is already in use")

        moved = translate_zone(original, dx=dx, dy=dy)
        copy = moved.model_copy(update={"id": new_id, "label": f"{original.label} (copy)"})
        self.add_zones((copy,))
        return copy

    def set_bubble_override(
        self, zone_id: str, *, row: int, column: int, center_x: float, center_y: float
    ) -> None:
        """Set (or replace) an explicit centre for one bubble, for fine-tuning.

        Raises:
            KeyError: No zone has that id, or it has no bubble grid.
        """
        zone = self.template.zone_by_id(zone_id)
        if zone is None or zone.grid is None:
            raise KeyError(f"No grid zone with id '{zone_id}'")

        remaining = tuple(
            override
            for override in zone.grid.overrides
            if not (override.row == row and override.column == column)
        )
        new_override = BubbleOverride(
            row=row, column=column, center=NormalizedPoint(x=center_x, y=center_y)
        )
        new_grid = BubbleGrid(
            origin=zone.grid.origin,
            row_pitch=zone.grid.row_pitch,
            column_pitch=zone.grid.column_pitch,
            bubble_size=zone.grid.bubble_size,
            overrides=(*remaining, new_override),
        )
        self.replace_zone(zone_id, zone.model_copy(update={"grid": new_grid}))

    def clear_bubble_override(self, zone_id: str, *, row: int, column: int) -> None:
        """Remove a bubble override, reverting that cell to its computed position."""
        zone = self.template.zone_by_id(zone_id)
        if zone is None or zone.grid is None:
            raise KeyError(f"No grid zone with id '{zone_id}'")

        remaining = tuple(
            override
            for override in zone.grid.overrides
            if not (override.row == row and override.column == column)
        )
        new_grid = BubbleGrid(
            origin=zone.grid.origin,
            row_pitch=zone.grid.row_pitch,
            column_pitch=zone.grid.column_pitch,
            bubble_size=zone.grid.bubble_size,
            overrides=remaining,
        )
        self.replace_zone(zone_id, zone.model_copy(update={"grid": new_grid}))


__all__ = ["MANUAL_CONFIRMED", "MISSING", "DesignerState", "DetectionMethod", "MarkerStatus"]

