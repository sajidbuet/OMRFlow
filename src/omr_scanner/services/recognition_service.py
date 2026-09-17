"""Recognising one scanned sheet, end to end.

Purpose:
    Join the three halves of Phase 3 into the single operation the rest of the
    application asks for: "read this image with this template". Alignment
    (Phase 1), bubble measurement (:mod:`omr_scanner.imaging.metrics`) and
    interpretation (:mod:`omr_scanner.recognition`) each know nothing about the
    others; this module is where they meet.

Responsibilities:
    * :func:`recognise_scan` - load, align, measure, interpret, and report.
    * Translate every result into **plain data** - strings, floats, bytes - so
      that the Scan page can display it without importing ``numpy``,
      ``omr_scanner.imaging`` or ``omr_scanner.recognition``, all of which are
      forbidden to the GUI layer (``docs/ARCHITECTURE.md``).
    * Never raise for an unreadable or unalignable sheet: a batch has to
      continue, so a failure becomes a *result* with a status and a message.

What does NOT belong here:
    * Any pixel algorithm or decision threshold of its own. Both come from the
      layers below, and every threshold ultimately from the template.
    * File naming, copying or CSV writing; those are
      :mod:`omr_scanner.services.filename_manager`,
      :mod:`omr_scanner.services.batch_processor` and
      :mod:`omr_scanner.reporting.scan_csv`.
    * Qt of any kind.

Coordinate systems, in one place:
    Three frames are in play and confusing them is the classic OMR bug.

    * **Source pixels** - the scanner's own image, whatever size it happens to
      be. Only :attr:`ScanResult.markers` is expressed here, because that is
      where the registration marks were physically found.
    * **Normalised template coordinates** - ``0..1`` on the canonical page, what
      the ``.omrt`` document stores.
    * **Canonical pixels** - the rectified page,
      ``template.page.canonical_width_px`` by ``canonical_height_px``. Every
      overlay and every measurement in a :class:`ScanResult` is in this frame,
      which is also the frame :attr:`ScanResult.preview` is an image of, so the
      GUI can draw overlays directly over the preview with no further mapping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from omr_scanner.domain.template import IgnoredFieldDefinition
from omr_scanner.errors import ImagingError, OMRScannerError
from omr_scanner.imaging.alignment import align_sheet
from omr_scanner.imaging.metrics import (
    BubbleMeasurement,
    BubbleMetricsConfig,
    estimate_ink_level,
    measure_bubbles,
)
from omr_scanner.recognition.fields import recognise_template, zone_groups
from omr_scanner.recognition.models import FieldStatus, MarkStatus
from omr_scanner.services.alignment_service import alignment_config_from_template, load_scan_image
from omr_scanner.services.marker_detection_service import DecodedImage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate, Zone
    from omr_scanner.recognition.models import SheetRecognition

_LOGGER = logging.getLogger(__name__)

DEFAULT_PREVIEW_MAX_DIMENSION = 1400
"""Longest side of the preview image handed to the GUI, in pixels.

A rectified A4 page at 300 dpi is about 8 MB as grayscale bytes; a batch of a
hundred would be most of a gigabyte held for no reason, because only the
selected scan is ever on screen. Downscaling the *preview* costs nothing in
accuracy - every measurement was already taken at full resolution, and every
overlay coordinate stays in canonical pixels, which the GUI scales anyway."""


class RegistrationStatus(StrEnum):
    """How well the sheet could be mapped onto the template's canonical page."""

    REGISTERED = "registered"
    """Four markers found, orientation resolved, no warnings."""

    REGISTERED_WITH_WARNING = "registered_with_warning"
    """Usable, but something was closer to its limit than is comfortable - a
    faint orientation mark, a marker touching the image border, an unusual
    aspect ratio. The values are reported; the sheet is worth a look."""

    FAILED = "registration_failed"
    """The page could not be rectified. No values are produced at all: a sheet
    that was not registered cannot be measured, and inventing answers from an
    unaligned image is exactly the confidently-wrong result this pipeline
    exists to avoid."""


class RecognitionOutcome(StrEnum):
    """The overall outcome of reading one scan, as shown in the scan list."""

    PENDING = "pending"
    """Imported but not processed yet."""

    COMPLETE = "complete"
    """Every field and question resolved cleanly."""

    REVIEW = "review"
    """Read, but something needs a human: a blank, a double mark, a faint one."""

    REGISTRATION_FAILED = "registration_failed"
    """The page could not be aligned; see
    :attr:`ScanResult.registration_message`."""

    ERROR = "error"
    """The file could not be read at all, or an unexpected failure occurred."""


@dataclass(frozen=True, slots=True)
class MarkerView:
    """One registration marker, in the **source** scan's own pixels.

    Attributes:
        role: Canonical corner role (``"top_left"`` ...), as a plain string.
        x: Marker centre in source-image pixels.
        y: Marker centre in source-image pixels.
        score: Selection score in ``[0, 1]``.
    """

    role: str
    x: float
    y: float
    score: float


@dataclass(frozen=True, slots=True)
class BubbleView:
    """One measured bubble, in canonical pixels, ready to draw.

    Attributes:
        zone_id: The zone the bubble belongs to.
        row: Row index within the zone's bubble grid.
        column: Column index within the zone's bubble grid.
        label: The symbol this bubble stands for.
        x: Bubble centre on the canonical page.
        y: Bubble centre on the canonical page.
        width: Printed bubble width in canonical pixels.
        height: Printed bubble height in canonical pixels.
        fill_ratio: Measured ink fraction in ``[0, 1]``.
        selected: Whether the decision layer counted this bubble as marked.
        leading: Whether this was the darkest bubble of its group, selected or
            not. Lets the overlay show what an uncertain group nearly said, and
            gives each group exactly one anchor for its attention glyph.
        group_status: Status of the response group this bubble belongs to, as
            the string value of
            :class:`~omr_scanner.recognition.models.MarkStatus`. Lets the GUI
            colour a bubble by *why* its group needs attention without knowing
            anything about the recognition package.
    """

    zone_id: str
    row: int
    column: int
    label: str
    x: float
    y: float
    width: float
    height: float
    fill_ratio: float
    selected: bool
    leading: bool
    group_status: str


@dataclass(frozen=True, slots=True)
class ZoneView:
    """One template zone projected onto the canonical page, ready to draw.

    Attributes:
        zone_id: Template zone id.
        label: Human readable zone name.
        field_type: Field type as a plain string.
        x: Left edge on the canonical page, in pixels.
        y: Top edge on the canonical page, in pixels.
        width: Width in canonical pixels.
        height: Height in canonical pixels.
        color: The zone's ``#RRGGBB`` display colour from the template.
        status: Field status (or the worst question status inside a question
            block), as a plain string.
    """

    zone_id: str
    label: str
    field_type: str
    x: float
    y: float
    width: float
    height: float
    color: str
    status: str


@dataclass(frozen=True, slots=True)
class CharacterView:
    """One character position of a recognised field.

    Attributes:
        position: Zero-based printed position within the field.
        value: The symbol, ``""`` when blank, or ``"2-7"`` when doubly marked.
        status: :class:`~omr_scanner.recognition.models.MarkStatus` value.
        top_fill: Highest fill ratio in the group.
        margin: Separation between the darkest bubble and the next.
        confidence: Bounded confidence in ``[0, 1]``; see
            :func:`omr_scanner.recognition.decide.decide_group`.
    """

    position: int
    value: str
    status: str
    top_fill: float
    margin: float
    confidence: float


@dataclass(frozen=True, slots=True)
class FieldView:
    """One recognised non-question field, as the GUI shows it.

    Attributes:
        zone_id: Template zone id.
        label: Human readable name.
        field_type: Field type as a plain string.
        value: Assembled value; ``"?"`` marks an unresolved position and ``"_"``
            a blank one.
        status: :class:`~omr_scanner.recognition.models.FieldStatus` value.
        needs_review: Whether any position should be shown to a human.
        characters: Per-position detail.
    """

    zone_id: str
    label: str
    field_type: str
    value: str
    status: str
    needs_review: bool
    characters: tuple[CharacterView, ...]


@dataclass(frozen=True, slots=True)
class AnswerView:
    """One recognised question, as the GUI shows it.

    Attributes:
        number: Printed question number.
        zone_id: The question block it came from.
        value: ``""``, ``"B"``, or ``"B-D"`` for a double mark.
        status: :class:`~omr_scanner.recognition.models.MarkStatus` value.
        needs_review: Whether this answer should be queued for a human.
        top_fill: Highest fill ratio among the options.
        margin: Separation between the darkest option and the next.
        confidence: Bounded confidence in ``[0, 1]``.
    """

    number: int
    zone_id: str
    value: str
    status: str
    needs_review: bool
    top_fill: float
    margin: float
    confidence: float

    @property
    def display_value(self) -> str:
        """What to show in a compact list: the value, or ``"?"`` when unresolved.

        A blank answer shows as an empty cell, a resolved one as its label, a
        double mark as ``"B-D"``, and anything the engine could not decide as
        ``"?"`` - the convention the Phase 3 brief specifies for the on-screen
        indicator, while the exported value keeps the detail.
        """
        if self.status in (MarkStatus.UNCERTAIN.value, MarkStatus.UNREADABLE.value):
            return "?" if not self.value else f"{self.value}?"
        return self.value


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything known about one processed scan.

    Attributes:
        source_path: The image that was read.
        outcome: Overall outcome for the scan list.
        registration: How well the page was rectified.
        registration_message: Plain-language explanation, empty when the
            registration was clean.
        warnings: Alignment warnings, as plain strings.
        error_code: Stable machine-readable failure code from
            :class:`~omr_scanner.errors.ImagingError`, or ``""``.
        fields: Recognised non-question fields, in template order.
        answers: Recognised questions, ordered by number.
        identifier_zone_id: Which field is the candidate identifier, if any.
        set_code_zone_id: Which field is the set code, if any.
        zones: Zone rectangles for the overlay.
        bubbles: Measured bubbles for the overlay.
        markers: Registration markers, in *source* pixels.
        preview: Downscaled rectified page for display, or ``None`` when the
            caller did not ask for one.
        preview_scale: ``preview`` pixels per canonical pixel, so the GUI can
            map canonical overlay coordinates onto the preview.
        canonical_width: Canonical page width in pixels.
        canonical_height: Canonical page height in pixels.
        source_width: Source scan width in pixels.
        source_height: Source scan height in pixels.
        elapsed_seconds: Wall-clock duration of the whole operation.
    """

    source_path: Path
    outcome: RecognitionOutcome
    registration: RegistrationStatus
    registration_message: str = ""
    warnings: tuple[str, ...] = ()
    error_code: str = ""
    fields: tuple[FieldView, ...] = ()
    answers: tuple[AnswerView, ...] = ()
    identifier_zone_id: str | None = None
    set_code_zone_id: str | None = None
    zones: tuple[ZoneView, ...] = ()
    bubbles: tuple[BubbleView, ...] = ()
    markers: tuple[MarkerView, ...] = ()
    preview: DecodedImage | None = field(default=None, repr=False)
    preview_scale: float = 1.0
    canonical_width: int = 0
    canonical_height: int = 0
    source_width: int = 0
    source_height: int = 0
    elapsed_seconds: float = 0.0

    # ------------------------------------------------------------------
    # Convenience accessors used by the GUI, the CSV export and the tests
    # ------------------------------------------------------------------
    def field_view(self, zone_id: str | None) -> FieldView | None:
        """Return the field for ``zone_id``, or ``None``."""
        if zone_id is None:
            return None
        return next((item for item in self.fields if item.zone_id == zone_id), None)

    @property
    def identifier(self) -> FieldView | None:
        """The candidate identifier field (roll number), when there is one."""
        return self.field_view(self.identifier_zone_id)

    @property
    def set_code(self) -> FieldView | None:
        """The set-code field, when there is one."""
        return self.field_view(self.set_code_zone_id)

    @property
    def identifier_value(self) -> str:
        """The identifier as recognised, or ``""`` when the template has none."""
        found = self.identifier
        return found.value if found is not None else ""

    @property
    def set_code_value(self) -> str:
        """The set code as recognised, or ``""``."""
        found = self.set_code
        return found.value if found is not None else ""

    @property
    def identifier_is_reliable(self) -> bool:
        """Whether the identifier may be used as a file name.

        True only when the sheet registered *and* every character position of
        the identifier resolved. A roll number with one uncertain digit is not
        a file name; it is a review item.
        """
        found = self.identifier
        return (
            self.registration is not RegistrationStatus.FAILED
            and found is not None
            and found.status == FieldStatus.RESOLVED.value
        )

    @property
    def review_count(self) -> int:
        """How many fields and answers need a human decision."""
        return sum(item.needs_review for item in self.fields) + sum(
            answer.needs_review for answer in self.answers
        )

    @property
    def warning_count(self) -> int:
        """Alignment warnings plus items needing review - the scan list's number."""
        return len(self.warnings) + self.review_count


def _downscaled_preview(
    image: NDArray[np.uint8], max_dimension: int | None
) -> tuple[DecodedImage, float]:
    """Return a display copy of the rectified page and its scale factor.

    Uses area averaging, which is the correct filter for shrinking: it keeps a
    faint pencil mark visible instead of letting point sampling miss it.
    """
    import cv2  # local import: only the preview path needs OpenCV here

    height, width = int(image.shape[0]), int(image.shape[1])
    scale = 1.0
    if max_dimension is not None and max(width, height) > max_dimension:
        scale = max_dimension / float(max(width, height))
        target = (max(round(width * scale), 1), max(round(height * scale), 1))
        resized = cv2.resize(image, target, interpolation=cv2.INTER_AREA)
    else:
        resized = image

    contiguous = np.ascontiguousarray(resized)
    out_height, out_width = int(contiguous.shape[0]), int(contiguous.shape[1])
    channels = int(contiguous.shape[2]) if contiguous.ndim == 3 else 1
    return (
        DecodedImage(
            width=out_width,
            height=out_height,
            channels=channels,
            stride=out_width * channels,
            data=contiguous.tobytes(),
        ),
        scale,
    )


def _zone_measurements(
    page: NDArray[np.uint8],
    zone: Zone,
    *,
    canonical_width: int,
    canonical_height: int,
    ink_level: float,
    metrics_config: BubbleMetricsConfig,
) -> dict[tuple[int, int], BubbleMeasurement]:
    """Measure every bubble of one zone, keyed by its ``(row, column)``.

    The template stores normalised coordinates; this is the single place they
    become canonical pixels, and it is deliberately arithmetic rather than a
    search: the page has already been rectified onto exactly those coordinates,
    so a bubble that is not where the template says it is means the template is
    wrong, not that the sheet should be hunted through.
    """
    grid = zone.grid
    if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
        return {}

    width_px = grid.bubble_size.width * canonical_width
    height_px = grid.bubble_size.height * canonical_height

    cells = [
        (row, column)
        for row in range(zone.field.rows)
        for column in range(zone.field.columns)
    ]
    centers = []
    for row, column in cells:
        center = grid.bubble_center(row, column)
        centers.append((center.x * canonical_width, center.y * canonical_height))

    measurements = measure_bubbles(
        page,
        centers,
        width_px=width_px,
        height_px=height_px,
        ink_level=ink_level,
        config=metrics_config,
    )
    return dict(zip(cells, measurements, strict=True))


def _build_views(
    template: OmrTemplate,
    recognition: SheetRecognition,
    measurements: dict[str, dict[tuple[int, int], BubbleMeasurement]],
    *,
    canonical_width: int,
    canonical_height: int,
) -> tuple[
    tuple[FieldView, ...],
    tuple[AnswerView, ...],
    tuple[ZoneView, ...],
    tuple[BubbleView, ...],
]:
    """Project a recognition result into the plain types the GUI consumes."""
    fields = tuple(
        FieldView(
            zone_id=item.zone_id,
            label=item.label,
            field_type=item.field_type,
            value=item.value,
            status=item.status.value,
            needs_review=item.needs_review,
            characters=tuple(
                CharacterView(
                    position=position,
                    value=group.value,
                    status=group.status.value,
                    top_fill=group.top_fill,
                    margin=group.margin,
                    confidence=group.confidence,
                )
                for position, group in enumerate(item.groups)
            ),
        )
        for item in recognition.fields
    )

    answers = tuple(
        AnswerView(
            number=answer.number,
            zone_id=answer.zone_id,
            value=answer.value,
            status=answer.status.value,
            needs_review=answer.needs_review,
            top_fill=answer.decision.top_fill,
            margin=answer.decision.margin,
            confidence=answer.decision.confidence,
        )
        for answer in recognition.answers
    )

    # Per-zone worst status, so one overlay rectangle can carry the state of a
    # whole question block without the GUI scanning every answer itself.
    zone_status: dict[str, str] = {item.zone_id: item.status.value for item in recognition.fields}
    for answer in recognition.answers:
        current = zone_status.get(answer.zone_id)
        zone_status[answer.zone_id] = _worse_status(current, answer.status.value)

    zones = tuple(
        ZoneView(
            zone_id=zone.id,
            label=zone.label,
            field_type=str(zone.field.type.value),
            x=zone.bounds.x * canonical_width,
            y=zone.bounds.y * canonical_height,
            width=zone.bounds.width * canonical_width,
            height=zone.bounds.height * canonical_height,
            color=zone.display_color,
            status=zone_status.get(zone.id, ""),
        )
        for zone in template.zones
    )

    # Which bubbles the decision layer selected, and how each group ended up.
    # `zone_groups` yields groups in the same order the recognition layer
    # decided them, so the two zip together without any lookup key.
    selected_cells: dict[str, set[tuple[int, int]]] = {}
    leading_cells: dict[str, set[tuple[int, int]]] = {}
    cell_status: dict[str, dict[tuple[int, int], str]] = {}
    field_groups = {item.zone_id: item.groups for item in recognition.fields}

    for zone in template.zones:
        if isinstance(zone.field, IgnoredFieldDefinition):
            continue
        decisions = field_groups.get(zone.id)
        if decisions is None:
            decisions = tuple(
                answer.decision
                for answer in sorted(
                    (a for a in recognition.answers if a.zone_id == zone.id),
                    key=lambda answer: answer.number,
                )
            )
        chosen = selected_cells.setdefault(zone.id, set())
        statuses = cell_status.setdefault(zone.id, {})
        leaders = leading_cells.setdefault(zone.id, set())
        for group, decision in zip(zone_groups(zone), decisions, strict=True):
            for index, cell in enumerate(group.cells):
                statuses[cell] = decision.status.value
                if index in decision.selected:
                    chosen.add(cell)
                if index == decision.leading_index:
                    leaders.add(cell)

    bubbles: list[BubbleView] = []
    for zone in template.zones:
        grid = zone.grid
        if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        labels_by_cell = {
            cell: group.labels[index]
            for group in zone_groups(zone)
            for index, cell in enumerate(group.cells)
        }
        zone_measure = measurements.get(zone.id, {})
        for (row, column), measurement in sorted(zone_measure.items()):
            bubbles.append(
                BubbleView(
                    zone_id=zone.id,
                    row=row,
                    column=column,
                    label=labels_by_cell.get((row, column), ""),
                    x=measurement.center_x,
                    y=measurement.center_y,
                    width=grid.bubble_size.width * canonical_width,
                    height=grid.bubble_size.height * canonical_height,
                    fill_ratio=measurement.fill_ratio,
                    selected=(row, column) in selected_cells.get(zone.id, set()),
                    leading=(row, column) in leading_cells.get(zone.id, set()),
                    group_status=cell_status.get(zone.id, {}).get((row, column), ""),
                )
            )

    return fields, answers, zones, tuple(bubbles)


_STATUS_SEVERITY: dict[str, int] = {
    FieldStatus.RESOLVED.value: 0,
    MarkStatus.RESOLVED.value: 0,
    FieldStatus.BLANK.value: 1,
    MarkStatus.BLANK.value: 1,
    FieldStatus.INCOMPLETE.value: 2,
    FieldStatus.UNCERTAIN.value: 3,
    MarkStatus.UNCERTAIN.value: 3,
    FieldStatus.MULTIPLE.value: 4,
    MarkStatus.MULTIPLE.value: 4,
    FieldStatus.UNREADABLE.value: 5,
    MarkStatus.UNREADABLE.value: 5,
}
"""How loudly each status should speak when several are summarised into one.

Ordered by how much human attention the state deserves, not alphabetically: an
unreadable region outranks a double mark, which outranks a faint one, which
outranks a gap."""


def _worse_status(current: str | None, candidate: str) -> str:
    """Return whichever of two status strings deserves more attention."""
    if current is None:
        return candidate
    if _STATUS_SEVERITY.get(candidate, 0) > _STATUS_SEVERITY.get(current, 0):
        return candidate
    return current


def _registration_status(warnings: tuple[str, ...]) -> RegistrationStatus:
    """Map alignment warnings onto a registration status."""
    if warnings:
        return RegistrationStatus.REGISTERED_WITH_WARNING
    return RegistrationStatus.REGISTERED


def _describe_warnings(warnings: tuple[str, ...]) -> str:
    """Render alignment warnings as one plain-language sentence."""
    if not warnings:
        return ""
    readable = ", ".join(warning.replace("_", " ").lower() for warning in warnings)
    return f"Registered, but with reservations: {readable}."


def recognise_scan(
    path: Path,
    template: OmrTemplate,
    *,
    metrics_config: BubbleMetricsConfig | None = None,
    with_preview: bool = True,
    preview_max_dimension: int | None = DEFAULT_PREVIEW_MAX_DIMENSION,
) -> ScanResult:
    """Read one scanned sheet with one template.

    Never raises for a bad sheet: a file that cannot be read, a page that cannot
    be registered and an unexpected internal failure all come back as a
    :class:`ScanResult` carrying an outcome and a message, because a batch must
    survive any one of its files.

    Args:
        path: Image file to read.
        template: The template describing the sheet.
        metrics_config: Bubble sampling tuning; defaults apply when omitted.
        with_preview: Produce a display image of the rectified page. Batch
            processing turns this off - only the selected scan is ever shown,
            and a hundred full-page previews is a gigabyte held for nothing.
        preview_max_dimension: Longest side of that preview.

    Returns:
        The result. ``outcome`` is
        :attr:`RecognitionOutcome.REGISTRATION_FAILED` or
        :attr:`RecognitionOutcome.ERROR` when nothing could be read.
    """
    started = time.perf_counter()
    settings = metrics_config if metrics_config is not None else BubbleMetricsConfig()

    try:
        image = load_scan_image(path, color=False)
    except OMRScannerError as exc:
        _LOGGER.warning("Scan %s could not be loaded: %s", path.name, exc)
        return ScanResult(
            source_path=path,
            outcome=RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            registration_message=exc.user_message,
            error_code=getattr(exc, "code", "") or "",
            elapsed_seconds=time.perf_counter() - started,
        )

    source_height, source_width = int(image.shape[0]), int(image.shape[1])

    try:
        alignment = align_sheet(image, config=alignment_config_from_template(template))
    except ImagingError as exc:
        _LOGGER.info(
            "Scan %s failed registration (%s): %s", path.name, exc.code, exc
        )
        return ScanResult(
            source_path=path,
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            registration_message=exc.user_message,
            error_code=exc.code,
            source_width=source_width,
            source_height=source_height,
            canonical_width=template.page.canonical_width_px,
            canonical_height=template.page.canonical_height_px,
            elapsed_seconds=time.perf_counter() - started,
        )

    page = alignment.normalized_image
    canonical_height, canonical_width = int(page.shape[0]), int(page.shape[1])

    # The "what does solid ink look like on this page" estimate is a property of
    # the page, not of a zone, so it is measured once and shared: measuring it
    # per zone would let a zone whose bubbles are all empty set a threshold from
    # its own printed glyphs and read them as marks.
    page_ink = estimate_ink_level(page, config=settings)
    measurements = {
        zone.id: _zone_measurements(
            page,
            zone,
            canonical_width=canonical_width,
            canonical_height=canonical_height,
            ink_level=page_ink,
            metrics_config=settings,
        )
        for zone in template.zones
        if not isinstance(zone.field, IgnoredFieldDefinition)
    }
    recognition = recognise_template(template, measurements)

    fields, answers, zones, bubbles = _build_views(
        template,
        recognition,
        measurements,
        canonical_width=canonical_width,
        canonical_height=canonical_height,
    )

    preview: DecodedImage | None = None
    preview_scale = 1.0
    if with_preview:
        preview, preview_scale = _downscaled_preview(page, preview_max_dimension)

    warnings = tuple(warning.value for warning in alignment.warnings)
    registration = _registration_status(warnings)
    needs_review = recognition.review_count > 0
    outcome = RecognitionOutcome.REVIEW if needs_review else RecognitionOutcome.COMPLETE

    _LOGGER.info(
        "Scan %s: registration=%s warnings=%d review_items=%d multiple=%d blank=%d",
        path.name,
        registration.value,
        len(warnings),
        recognition.review_count,
        recognition.multiple_mark_count,
        recognition.blank_answer_count,
    )

    return ScanResult(
        source_path=path,
        outcome=outcome,
        registration=registration,
        registration_message=_describe_warnings(warnings),
        warnings=warnings,
        fields=fields,
        answers=answers,
        identifier_zone_id=recognition.identifier_zone_id,
        set_code_zone_id=recognition.set_code_zone_id,
        zones=zones,
        bubbles=bubbles,
        markers=tuple(
            MarkerView(
                role=str(detection.role.value),
                x=detection.center.x,
                y=detection.center.y,
                score=detection.score,
            )
            for detection in alignment.corner_markers
        ),
        preview=preview,
        preview_scale=preview_scale,
        canonical_width=canonical_width,
        canonical_height=canonical_height,
        source_width=source_width,
        source_height=source_height,
        elapsed_seconds=time.perf_counter() - started,
    )


__all__ = [
    "DEFAULT_PREVIEW_MAX_DIMENSION",
    "AnswerView",
    "BubbleView",
    "CharacterView",
    "FieldView",
    "MarkerView",
    "RecognitionOutcome",
    "RegistrationStatus",
    "ScanResult",
    "ZoneView",
    "recognise_scan",
]
