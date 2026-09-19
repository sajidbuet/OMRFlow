"""What one recognised sheet *is*, as plain data.

Purpose:
    Define the contract between the recognition engine and everything that
    consumes it - the Scan page today, conflict review, scoring and reporting in
    later phases, a regression harness, a JSON file on disk. Nothing here
    computes anything; this module is the vocabulary.

Responsibilities:
    * :class:`ScanResult` and the view types it contains - the *only* thing a
      caller ever receives from recognition.
    * :class:`StatusCode`: machine-readable conditions, so no consumer has to
      match on English prose.
    * :class:`ScanQuality` and :class:`StageTimings`: diagnostic measurements
      that travel with the result but never influence it.
    * JSON serialisation, because a result has to survive being written to a
      fixture, a benchmark report or a future review queue.

What does NOT belong here:
    * Pixels, OpenCV, NumPy, Qt. Everything below is strings, numbers, booleans
      and tuples of the same - which is exactly why the GUI layer may import it
      while being forbidden `cv2`, `numpy`, `imaging` and `recognition`.
    * Thresholds and decisions. Those belong to the template and to
      :mod:`omr_scanner.recognition` respectively.

Why the engine is versioned:
    :data:`ENGINE_VERSION` identifies the *behaviour* that produced a result and
    :data:`RESULT_SCHEMA_VERSION` the *shape* it was written in. They move
    independently: a recalibrated Recognition Engine v2 reading the same sheet
    differently bumps the first; adding a field to the serialised form bumps the
    second. A benchmark comparing two engines, and a Phase 4 review screen
    reading a stored result, each need one of them and neither needs the
    application's release number.

Stability promise (this is a contract, not an implementation detail):
    Fields are **added**, never removed or repurposed, within a schema version.
    Every field added since the first release carries a default, so a consumer
    written against an older version keeps working and a result written by an
    older version still loads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.recognition.models import FieldStatus, MarkStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.services.marker_detection_service import DecodedImage

ENGINE_NAME = "omrflow-recognition"
"""Identifier of the recognition engine that produces these results."""

ENGINE_VERSION = "1.0"
"""Version of the recognition *behaviour*.

Bumped when the engine would read the same sheet differently: a changed
default threshold, a new measurement, a different decision rule. Deliberately
not the application's release number - the point of this string is to let a
benchmark say "engine 1.0 scored X on this dataset, engine 1.1 scored Y", which
is meaningless if it also changes when the About box does."""

RESULT_SCHEMA_VERSION = 1
"""Version of the serialised :class:`ScanResult` shape. See the module docstring."""


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


class StatusCode(StrEnum):
    """Machine-readable conditions attached to one result.

    A result carries *several* of these at once, because a sheet is routinely
    several things at the same time: registered with a warning, and carrying one
    double mark, and with an unreadable roll number. Collapsing that into one
    enum would force a precedence order on conditions that a caller may want to
    filter independently.

    These names are a stable API. They exist so that a batch report, a future
    conflict queue and a benchmark can branch on a condition without matching on
    an English sentence, and so that the sentence can be rewritten - or
    translated - without breaking anything.
    """

    OK = "OK"
    """Nothing needs attention."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    """At least one group was read, but too faint or too close to its runner-up
    to be accepted; see :attr:`GroupDecision.confidence`."""

    BLANK = "BLANK"
    """At least one question carries no mark at all."""

    MULTIPLE_MARK = "MULTIPLE_MARK"
    """At least one group carries more than one mark. Both are kept."""

    AMBIGUOUS = "AMBIGUOUS"
    """At least one group could not be resolved into a value."""

    ALIGNMENT_WARNING = "ALIGNMENT_WARNING"
    """The page registered, but with a reservation worth a look."""

    ALIGNMENT_FAILED = "ALIGNMENT_FAILED"
    """The page could not be rectified, so nothing was measured."""

    ORIENTATION_FAILED = "ORIENTATION_FAILED"
    """Which way up the page is could not be established."""

    MARKER_NOT_FOUND = "MARKER_NOT_FOUND"
    """One or more registration markers could not be located."""

    ROLL_UNREADABLE = "ROLL_UNREADABLE"
    """The candidate identifier did not fully resolve, so it must not be used as
    an identity or a file name."""

    SET_UNREADABLE = "SET_UNREADABLE"
    """The question-paper set code did not fully resolve."""

    INVALID_TEMPLATE = "INVALID_TEMPLATE"
    """The template cannot describe this sheet - no zones, or geometry the
    engine cannot apply."""

    IMAGE_LOAD_ERROR = "IMAGE_LOAD_ERROR"
    """The file could not be decoded as an image."""

    PROCESSING_ERROR = "PROCESSING_ERROR"
    """An unexpected failure. The batch continues; this scan does not."""


IMAGING_ERROR_STATUS: dict[str, StatusCode] = {
    "INVALID_IMAGE": StatusCode.IMAGE_LOAD_ERROR,
    "INSUFFICIENT_MARKERS": StatusCode.MARKER_NOT_FOUND,
    "AMBIGUOUS_MARKERS": StatusCode.MARKER_NOT_FOUND,
    "MARKER_DETECTION_FAILED": StatusCode.MARKER_NOT_FOUND,
    "ORIENTATION_NOT_FOUND": StatusCode.ORIENTATION_FAILED,
    "INVALID_PAGE_GEOMETRY": StatusCode.ALIGNMENT_FAILED,
    "ALIGNMENT_TRANSFORM_FAILED": StatusCode.ALIGNMENT_FAILED,
    "IMAGING_ERROR": StatusCode.ALIGNMENT_FAILED,
}
"""Maps :class:`~omr_scanner.errors.ImagingError` codes onto status codes.

A table rather than string surgery on the class name, because the imaging codes
are themselves a stable API and the two vocabularies are allowed to differ: the
imaging layer distinguishes *why* marker selection failed, while a consumer of a
result usually only needs to know that it did."""


@dataclass(frozen=True, slots=True)
class MarkerView:
    """One registration marker, in the **source** scan's own pixels.

    Attributes:
        role: Canonical corner role (``"top_left"`` ...), as a plain string.
        x: Marker centre in source-image pixels.
        y: Marker centre in source-image pixels.
        score: Selection score in ``[0, 1]``.
        canonical_x: The same detected centre, mapped through the fitted
            homography into canonical pixels - the frame every zone and bubble
            overlay is drawn in. Zero when the transform was never computed
            (a failed registration carries no markers at all).
        canonical_y: As ``canonical_x``.
        expected_x: Where the template says this role's marker centre should
            sit, in canonical pixels - a property of the template alone, not
            of this scan. Comparing it with ``canonical_x`` is what a
            calibration overlay draws as "expected" versus "detected"
            (:mod:`omr_scanner.services.calibration_service`); with only four
            correspondences the homography fits them almost exactly, so a
            visible gap here says more about *how* the fit was constrained
            than about geometric error - see
            :attr:`~omr_scanner.services.recognition_models.ScanQuality.mean_reprojection_error_px`.
        expected_y: As ``expected_x``.
    """

    role: str
    x: float
    y: float
    score: float
    canonical_x: float = 0.0
    canonical_y: float = 0.0
    expected_x: float = 0.0
    expected_y: float = 0.0


@dataclass(frozen=True, slots=True)
class BubbleView:
    """One measured bubble, in canonical pixels, with the evidence behind it.

    This is the record that makes a later recalibration possible without
    re-reading the image: given ``fill_ratio`` and ``ink_threshold`` for every
    bubble of a batch, a different threshold can be evaluated arithmetically.

    Attributes:
        zone_id: The zone the bubble belongs to.
        row: Row index within the zone's bubble grid.
        column: Column index within the zone's bubble grid.
        label: The symbol this bubble stands for.
        x: Bubble centre on the canonical page.
        y: Bubble centre on the canonical page.
        width: Printed bubble width in canonical pixels - the size of the ring
            printed on the paper, **not** the region that was measured.
        height: Printed bubble height in canonical pixels.
        sample_half_width: Horizontal half-axis of the elliptical interior the
            sampler actually read, in canonical pixels. Deliberately smaller
            than ``width / 2``: the printed ring is ink, so measuring the full
            bubble would score an empty one as partly filled
            (:mod:`omr_scanner.imaging.metrics`). Anything drawing "the region
            recognition measured" must use this pair, and anything drawing
            "the printed bubble" must use ``width``/``height``; showing one
            and calling it the other is how a calibration overlay ends up
            looking convincing while describing a region the engine never
            read. Zero on a result whose caller did not keep the per-bubble
            evidence.
        sample_half_height: Vertical half-axis of the same ellipse.
        fill_ratio: Fraction of the sampled interior classified as ink - the
            quantity the fill threshold is expressed in.
        selected: Whether the decision layer counted this bubble as marked.
        leading: Whether this was the darkest bubble of its group, selected or
            not. Lets the overlay show what an uncertain group nearly said, and
            gives each group exactly one anchor for its attention glyph.
        group_status: Status of the response group this bubble belongs to, as
            the string value of
            :class:`~omr_scanner.recognition.models.MarkStatus`.
        mean_darkness: ``1 - mean(sample)/255`` over the sampled interior. Two
            bubbles can share a fill ratio and differ here - a hard pencil
            covering the whole bubble lightly, against a pen covering half of it
            - which is why both are kept.
        contrast: ``(paper_level - mean(sample)) / 255``, in ``[-1, 1]``: how
            much darker the interior is than the paper measured in an annulus
            around it. Negative when the interior is *brighter* than its
            surroundings, which is what a hole or a scanner artefact looks like.
        paper_level: The local paper level, in grey levels, this bubble was
            measured against.
        ink_threshold: The grey level below which a pixel counted as ink for
            this bubble. Recorded because it is derived from the local paper
            level and the page's ink level, so it differs per bubble and cannot
            be reconstructed from the settings alone.
        sample_pixels: How many pixels the interior sample covered. A small
            number means the bubble sat near the page edge and the measurement
            is weak.
        usable: Whether the sample could be taken at all.
        rank: Position of this bubble's fill ratio within its group, ``0`` for
            the darkest. Cheap to record here and awkward to recompute later,
            because it depends on the grouping the template defines.
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
    mean_darkness: float = 0.0
    contrast: float = 0.0
    paper_level: float = 0.0
    ink_threshold: float = 0.0
    sample_pixels: int = 0
    usable: bool = True
    rank: int = 0
    sample_half_width: float = 0.0
    sample_half_height: float = 0.0


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
        confidence: Bounded decision score in ``[0, 1]``; see
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
    """One recognised non-question field, as a consumer sees it.

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
    """One recognised question, as a consumer sees it.

    Attributes:
        number: Printed question number.
        zone_id: The question block it came from.
        value: ``""``, ``"B"``, or ``"B-D"`` for a double mark.
        status: :class:`~omr_scanner.recognition.models.MarkStatus` value.
        needs_review: Whether this answer should be queued for a human.
        top_fill: Highest fill ratio among the options.
        margin: Separation between the darkest option and the next.
        confidence: Bounded decision score in ``[0, 1]``.
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
class ScanQuality:
    """Diagnostic measurements of the scan itself.

    None of these decides anything. They exist so that a batch of a thousand
    sheets can be sorted by "which scans look worst", and so that a future
    calibration can ask whether an error correlates with skew, illumination or
    blur rather than with the decision thresholds.

    Deliberately reported rather than enforced: a sheet is only *failed* when it
    genuinely cannot be processed, never because a quality number crossed a
    threshold nobody has validated yet.

    Attributes:
        marker_count: Registration markers selected (four on a clean sheet).
        min_marker_score: Weakest of their selection scores, in ``[0, 1]``.
        mean_reprojection_error_px: Mean distance between each detected marker
            mapped through the transform and its canonical target. With four
            correspondences the homography is exact, so this measures numerical
            conditioning rather than geometric accuracy.
        max_reprojection_error_px: Largest of those distances.
        aspect_ratio_deviation: ``detected/expected - 1`` for the marker
            rectangle; zero for a perfectly square-on scan.
        quadrilateral_area_ratio: Fraction of the scan the page occupies. Small
            values mean wasted resolution.
        quarter_turns: Whole quarter turns applied to bring the page upright.
        rotation_degrees: Residual rotation the transform corrected, after those
            quarter turns, estimated from the homography.
        skew_degrees: Departure from perpendicularity between the page's two
            axes, estimated from the same matrix.
        perspective_strength: Magnitude of the projective part of the transform,
            scaled by page size. Zero for a flat scan; grows with the tilt of a
            photographed page.
        orientation_confidence: How clearly the orientation mark was found.
        orientation_assumed: The orientation was *not* measured; the engine fell
            back to assuming the page was upright. Always worth a human's eye.
        source_width: Scan width in pixels.
        source_height: Scan height in pixels.
        brightness: Mean intensity of the rectified page, ``0..1``.
        contrast: Standard deviation of that intensity, ``0..1``.
        sharpness: Variance of the Laplacian, normalised by ``255**2``. Higher
            is sharper; a blurred scan collapses towards zero. Comparable
            between scans of the same design, not an absolute scale.
        working_scale: Factor detection ran at; ``1.0`` means full resolution.
    """

    marker_count: int = 0
    min_marker_score: float = 0.0
    mean_reprojection_error_px: float = 0.0
    max_reprojection_error_px: float = 0.0
    aspect_ratio_deviation: float = 0.0
    quadrilateral_area_ratio: float = 0.0
    quarter_turns: int = 0
    rotation_degrees: float = 0.0
    skew_degrees: float = 0.0
    perspective_strength: float = 0.0
    orientation_confidence: float = 0.0
    orientation_assumed: bool = False
    source_width: int = 0
    source_height: int = 0
    brightness: float = 0.0
    contrast: float = 0.0
    sharpness: float = 0.0
    working_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class StageTimings:
    """Where the time went, in seconds.

    Recorded always, because the measurement costs one clock read per stage and
    the alternative - guessing which stage is slow - has cost more than that in
    every project that has tried it.

    Attributes:
        load: Decoding the image file.
        register: Marker detection, orientation and the perspective correction.
        measure: Sampling every bubble.
        decide: Turning measurements into values.
        present: Building the view objects, the quality metrics and the preview.
        total: The whole call, including anything not attributed above.
    """

    load: float = 0.0
    register: float = 0.0
    measure: float = 0.0
    decide: float = 0.0
    present: float = 0.0
    total: float = 0.0


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
        status_codes: Every :class:`StatusCode` that applies, sorted. The
            machine-readable summary a consumer should branch on.
        fields: Recognised non-question fields, in template order.
        answers: Recognised questions, ordered by number.
        identifier_zone_id: Which field is the candidate identifier, if any.
        set_code_zone_id: Which field is the set code, if any.
        zones: Zone rectangles for the overlay.
        bubbles: Measured bubbles, with their evidence.
        markers: Registration markers, in *source* pixels.
        preview: Downscaled rectified page for display, or ``None`` when the
            caller did not ask for one. Never serialised: it is a picture, not
            data, and a JSON file full of base64 pixels helps nobody.
        preview_scale: ``preview`` pixels per canonical pixel, so a viewer can
            map canonical overlay coordinates onto the preview.
        canonical_width: Canonical page width in pixels.
        canonical_height: Canonical page height in pixels.
        source_width: Source scan width in pixels.
        source_height: Source scan height in pixels.
        elapsed_seconds: Wall-clock duration of the whole operation.
        engine_name: Which engine produced this.
        engine_version: Which version of it - see the module docstring.
        template_id: The template's stable id, so a result can be re-checked
            against the document that produced it.
        template_name: Its display name, for a report a human reads.
        template_version: The template document's format version.
        recognised_at: ISO-8601 UTC timestamp.
        quality: Diagnostic measurements of the scan, or ``None`` when they were
            not requested.
        timings: Per-stage durations.
        source_transform: The **inverse** of the fitted homography, row-major,
            nine values - the map from canonical page pixels back to this
            scan's own pixels. Empty when the page never registered, or when
            the matrix could not be inverted.

            Recorded so that a review interface can show *where on the original
            scan* a disputed mark is, which is otherwise impossible: every
            other coordinate in this result is canonical, and the original is
            not in that frame. It is the engine's own transform, not a second
            one - see :func:`~omr_scanner.services.recognition_service.map_canonical_to_source`,
            which is the only thing that applies it.
    """

    source_path: Path
    outcome: RecognitionOutcome
    registration: RegistrationStatus
    registration_message: str = ""
    warnings: tuple[str, ...] = ()
    error_code: str = ""
    status_codes: tuple[str, ...] = ()
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
    engine_name: str = ENGINE_NAME
    engine_version: str = ENGINE_VERSION
    template_id: str = ""
    template_name: str = ""
    template_version: int = 0
    recognised_at: str = ""
    quality: ScanQuality | None = None
    timings: StageTimings = field(default_factory=StageTimings)
    source_transform: tuple[float, ...] = ()

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

    def has_status(self, code: StatusCode | str) -> bool:
        """Whether ``code`` applies to this result."""
        return str(code) in self.status_codes

    def bubbles_for(self, zone_id: str) -> tuple[BubbleView, ...]:
        """Every measured bubble belonging to ``zone_id``, in grid order."""
        return tuple(item for item in self.bubbles if item.zone_id == zone_id)

    def answer(self, number: int) -> AnswerView | None:
        """Return the answer to question ``number``, or ``None``."""
        return next((item for item in self.answers if item.number == number), None)

    # ------------------------------------------------------------------
    # Serialisation - the contract Phase 4/5 and the benchmark harness read
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Return this result as JSON-safe plain data.

        The preview image is deliberately omitted (see :attr:`preview`).
        Everything else round-trips through :meth:`from_dict` unchanged.
        """
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "engine": {"name": self.engine_name, "version": self.engine_version},
            "recognised_at": self.recognised_at,
            "source_path": str(self.source_path),
            "source_name": self.source_path.name,
            "template": {
                "id": self.template_id,
                "name": self.template_name,
                "format_version": self.template_version,
            },
            "outcome": self.outcome.value,
            "registration": self.registration.value,
            "registration_message": self.registration_message,
            "warnings": list(self.warnings),
            "error_code": self.error_code,
            "status_codes": list(self.status_codes),
            "identifier_zone_id": self.identifier_zone_id,
            "set_code_zone_id": self.set_code_zone_id,
            "identifier_value": self.identifier_value,
            "set_code_value": self.set_code_value,
            "fields": [
                {
                    **_plain(item, exclude={"characters"}),
                    "characters": [_plain(character) for character in item.characters],
                }
                for item in self.fields
            ],
            "answers": [_plain(item) for item in self.answers],
            "zones": [_plain(item) for item in self.zones],
            "bubbles": [_plain(item) for item in self.bubbles],
            "markers": [_plain(item) for item in self.markers],
            "quality": _plain(self.quality) if self.quality is not None else None,
            "timings": _plain(self.timings),
            "geometry": {
                "canonical_width": self.canonical_width,
                "canonical_height": self.canonical_height,
                "source_width": self.source_width,
                "source_height": self.source_height,
            },
            "source_transform": [_encode_value(item) for item in self.source_transform],
            # Through the same encoder as every nested value, so the "JSON has
            # no NaN" rule holds for a top-level number too.
            "elapsed_seconds": _encode_value(self.elapsed_seconds),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScanResult:
        """Rebuild a result from :meth:`to_dict` output.

        Unknown keys are ignored and missing ones fall back to their defaults,
        so a document written by a newer or an older build of the same schema
        version still loads. A genuinely incompatible ``schema_version`` is
        refused rather than silently misread.

        Raises:
            ValueError: The payload declares a schema version this build does
                not understand.
        """
        version = int(payload.get("schema_version", RESULT_SCHEMA_VERSION))
        if version > RESULT_SCHEMA_VERSION:
            raise ValueError(
                f"Recognition result schema version {version} is newer than this "
                f"build understands (max {RESULT_SCHEMA_VERSION})"
            )

        engine = payload.get("engine") or {}
        template = payload.get("template") or {}
        geometry = payload.get("geometry") or {}
        quality = payload.get("quality")

        return cls(
            source_path=Path(payload.get("source_path", payload.get("source_name", ""))),
            outcome=RecognitionOutcome(payload.get("outcome", RecognitionOutcome.PENDING.value)),
            registration=RegistrationStatus(
                payload.get("registration", RegistrationStatus.FAILED.value)
            ),
            registration_message=payload.get("registration_message", ""),
            warnings=tuple(payload.get("warnings", ())),
            error_code=payload.get("error_code", ""),
            status_codes=tuple(payload.get("status_codes", ())),
            fields=tuple(
                FieldView(
                    **{
                        **_kwargs(FieldView, item, exclude={"characters"}),
                        "characters": tuple(
                            _build(CharacterView, character)
                            for character in item.get("characters", ())
                        ),
                    }
                )
                for item in payload.get("fields", ())
            ),
            answers=tuple(_build(AnswerView, item) for item in payload.get("answers", ())),
            identifier_zone_id=payload.get("identifier_zone_id"),
            set_code_zone_id=payload.get("set_code_zone_id"),
            zones=tuple(_build(ZoneView, item) for item in payload.get("zones", ())),
            bubbles=tuple(_build(BubbleView, item) for item in payload.get("bubbles", ())),
            markers=tuple(_build(MarkerView, item) for item in payload.get("markers", ())),
            canonical_width=int(geometry.get("canonical_width", 0)),
            canonical_height=int(geometry.get("canonical_height", 0)),
            source_width=int(geometry.get("source_width", 0)),
            source_height=int(geometry.get("source_height", 0)),
            # `or 0.0` and not a default argument: a non-finite duration was
            # written as null, and "unmeasurable" reads back as zero rather
            # than as a crash in the middle of loading a batch of results.
            elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
            engine_name=engine.get("name", ENGINE_NAME),
            engine_version=engine.get("version", ENGINE_VERSION),
            template_id=template.get("id", ""),
            template_name=template.get("name", ""),
            template_version=int(template.get("format_version", 0)),
            recognised_at=payload.get("recognised_at", ""),
            quality=_build(ScanQuality, quality) if quality else None,
            timings=_build(StageTimings, payload.get("timings") or {}),
            # `or 0.0` per element: a non-finite coefficient was written as
            # null, and a transform with a hole in it is unusable rather than a
            # reason to fail loading the whole result.
            source_transform=tuple(
                float(item or 0.0) for item in payload.get("source_transform", ())
            ),
        )


def utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string.

    UTC, not local time: a batch processed across a midnight boundary, or on a
    machine whose clock is set to a different zone from the one that later reads
    the results, must still sort correctly.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def derive_status_codes(
    *,
    outcome: RecognitionOutcome,
    registration: RegistrationStatus,
    error_code: str,
    identifier: FieldView | None,
    set_code: FieldView | None,
    answers: tuple[AnswerView, ...],
    fields_: tuple[FieldView, ...],
    has_zones: bool,
) -> tuple[str, ...]:
    """Summarise one result's condition as machine-readable codes.

    Derived from the existing states rather than decided separately, so the
    codes can never disagree with the values they describe. That is also why
    this is a function over a finished result and not a parallel judgement made
    during recognition.
    """
    codes: set[StatusCode] = set()

    if error_code:
        codes.add(IMAGING_ERROR_STATUS.get(error_code, StatusCode.PROCESSING_ERROR))
    if outcome is RecognitionOutcome.ERROR and not error_code:
        codes.add(StatusCode.PROCESSING_ERROR)
    if registration is RegistrationStatus.FAILED:
        codes.add(StatusCode.ALIGNMENT_FAILED)
    elif registration is RegistrationStatus.REGISTERED_WITH_WARNING:
        codes.add(StatusCode.ALIGNMENT_WARNING)
    if not has_zones:
        codes.add(StatusCode.INVALID_TEMPLATE)

    group_statuses = [answer.status for answer in answers]
    group_statuses.extend(
        character.status for item in fields_ for character in item.characters
    )
    if MarkStatus.MULTIPLE.value in group_statuses:
        codes.add(StatusCode.MULTIPLE_MARK)
    if MarkStatus.UNCERTAIN.value in group_statuses:
        codes.add(StatusCode.LOW_CONFIDENCE)
    if MarkStatus.UNREADABLE.value in group_statuses:
        codes.add(StatusCode.AMBIGUOUS)
    if any(answer.status == MarkStatus.BLANK.value for answer in answers):
        codes.add(StatusCode.BLANK)

    if identifier is not None and identifier.status != FieldStatus.RESOLVED.value:
        codes.add(StatusCode.ROLL_UNREADABLE)
    if set_code is not None and set_code.status != FieldStatus.RESOLVED.value:
        codes.add(StatusCode.SET_UNREADABLE)

    if not codes:
        codes.add(StatusCode.OK)
    return tuple(sorted(code.value for code in codes))


# ----------------------------------------------------------------------
# Serialisation helpers
# ----------------------------------------------------------------------
def _plain(item: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Return a flat dataclass as a JSON-safe dictionary.

    Generic rather than one hand-written encoder per view: every view type is a
    flat record of primitives, so a hand-written encoder would only be a longer
    way to write the same thing, and one that drifts the moment a field is
    added.
    """
    skip = exclude or set()
    payload: dict[str, Any] = {}
    for member in fields(item):
        if member.name in skip:
            continue
        value = getattr(item, member.name)
        payload[member.name] = _encode_value(value)
    return payload


def _encode_value(value: Any) -> Any:
    """Convert one field value into something ``json`` accepts."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        # JSON has no NaN or Infinity. A non-finite measurement means "not
        # measurable", which round-trips honestly as null rather than as a
        # number some parsers accept and others reject.
        return None
    return value


def _kwargs(
    cls: type, payload: dict[str, Any], *, exclude: set[str] | None = None
) -> dict[str, Any]:
    """Return the subset of ``payload`` that ``cls`` actually declares."""
    skip = exclude or set()
    names = {member.name for member in fields(cls)} - skip
    return {key: value for key, value in payload.items() if key in names}


def _build[T](cls: type[T], payload: dict[str, Any]) -> T:
    """Construct ``cls`` from ``payload``, ignoring keys it does not declare."""
    return cls(**_kwargs(cls, payload))


__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "IMAGING_ERROR_STATUS",
    "RESULT_SCHEMA_VERSION",
    "AnswerView",
    "BubbleView",
    "CharacterView",
    "FieldView",
    "MarkerView",
    "RecognitionOutcome",
    "RegistrationStatus",
    "ScanQuality",
    "ScanResult",
    "StageTimings",
    "StatusCode",
    "ZoneView",
    "derive_status_codes",
    "utc_timestamp",
]
