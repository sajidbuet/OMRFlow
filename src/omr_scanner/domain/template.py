"""The ``.omrt`` OMR template document model.

Purpose:
    Define the versioned, JSON-backed description of a sheet design: page
    geometry, registration markers, the orientation marker, zones, fields,
    bubble grids and recognition settings.

Responsibilities:
    * Declare the document shape and validate it (Pydantic).
    * Provide the pure geometry needed to locate a bubble inside a zone
      (:meth:`BubbleGrid.bubble_center`) - the one calculation both the future
      designer and the future recognition engine must agree on.

What does NOT belong here:
    * File I/O. Loading and saving ``.omrt`` files is
      :mod:`omr_scanner.services.template_service`.
    * Pixel measurements or image access. A template is resolution independent;
      see :mod:`omr_scanner.domain.geometry` for the coordinate convention.
    * Recognition *behaviour*. :class:`RecognitionSettings` values are declared
      here and stored, but nothing reads them before Phase 3.

Specification:
    ``docs/TEMPLATE_FORMAT.md`` is the human-readable specification and must be
    updated together with this module. An illustrative document lives at
    ``resources/templates/example_answer_sheet.omrt`` and is validated by
    ``tests/unit/test_template_model.py``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omr_scanner import __version__
from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize

TEMPLATE_FORMAT_ID: Final = "omrflow-template"
"""Magic string identifying an ``.omrt`` document."""

TEMPLATE_FORMAT_VERSION = 1
"""Current ``.omrt`` format version. Increment only for breaking changes."""

TEMPLATE_FILE_SUFFIX = ".omrt"

_FALLBACK_DEFAULT_BUBBLE_RADIUS = 0.011
"""Bubble radius assumed when a document predates
:attr:`OmrTemplate.default_bubble_radius` - half the ``0.022`` normalised bubble
width every Phase 2 region dialog hard-coded, so such a document's geometry is
unchanged. Re-exported as
:data:`omr_scanner.domain.template_authoring.DEFAULT_BUBBLE_RADIUS`, which is the
name application code should use; it lives here because the model's own
:meth:`OmrTemplate.default_bubble_size` needs it and ``template_authoring``
imports this module, not the other way round."""

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class MarkerRole(StrEnum):
    """Which corner a registration marker belongs to, in the canonical frame."""

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_LEFT = "bottom_left"


class MarkerShape(StrEnum):
    """Printed shape of a marker, used by Phase 1 detection to pick a detector."""

    FILLED_SQUARE = "filled_square"
    FILLED_CIRCLE = "filled_circle"
    FILLED_RECTANGLE = "filled_rectangle"


class FieldType(StrEnum):
    """Logical meaning of a zone's contents."""

    NUMERIC = "numeric"
    """Digits 0-9, one column per digit (roll numbers, registration numbers)."""

    ALPHANUMERIC = "alphanumeric"
    """A configurable symbol set, one column per character."""

    SET_CODE = "set_code"
    """Question paper set/booklet identifier; usually a single column."""

    QUESTION_BLOCK = "question_block"
    """A run of MCQ questions sharing one set of answer labels."""

    IGNORED = "ignored"
    """A region deliberately excluded from recognition (logos, instructions)."""


class SymbolAxis(StrEnum):
    """Direction along which a grid's symbols are printed.

    ``VERTICAL`` is the common roll-number layout: each column is one character
    position and the digits 0-9 run down the page. ``HORIZONTAL`` is the common
    MCQ layout: each row is one question and the options A-D run across.
    """

    VERTICAL = "vertical"
    HORIZONTAL = "horizontal"


class PageGeometry(BaseModel):
    """Canonical page description a template's coordinates refer to.

    Attributes:
        width_mm: Physical paper width, for reference and printing checks.
        height_mm: Physical paper height.
        canonical_width_px: Width of the rectified image produced by the
            Phase 1 normalisation step. Zone coordinates are normalised, so this
            only fixes the working resolution and the aspect ratio.
        canonical_height_px: Height of the rectified image.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    width_mm: float = Field(gt=0.0)
    height_mm: float = Field(gt=0.0)
    canonical_width_px: int = Field(gt=0)
    canonical_height_px: int = Field(gt=0)

    @property
    def aspect_ratio(self) -> float:
        """Canonical width divided by canonical height."""
        return self.canonical_width_px / self.canonical_height_px


class RegistrationMarker(BaseModel):
    """A printed corner marker used to rectify a scan.

    Four of these define the perspective transform from a raw scan to the
    canonical page. ``search_radius`` bounds how far Phase 1 detection may look
    from the expected centre before declaring the marker missing; it is
    normalised like every other length in the document.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MarkerRole
    shape: MarkerShape = MarkerShape.FILLED_SQUARE
    center: NormalizedPoint
    size: NormalizedSize
    search_radius: float = Field(default=0.05, gt=0.0, le=0.5)


class OrientationMarker(BaseModel):
    """The single asymmetric marker that resolves 180 degree ambiguity.

    Four corner markers alone cannot tell an upright sheet from one fed upside
    down, because the corner pattern is symmetric. One extra marker placed near
    a known corner breaks the symmetry. ``expected_near`` names the corner it
    must appear beside once the sheet is upright.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: MarkerShape = MarkerShape.FILLED_RECTANGLE
    center: NormalizedPoint
    size: NormalizedSize
    expected_near: MarkerRole = MarkerRole.TOP_LEFT
    search_radius: float = Field(default=0.05, gt=0.0, le=0.5)


class RecognitionSettings(BaseModel):
    """Thresholds controlling how bubble measurements become decisions.

    Declared in Phase 0 so the format is stable; **no code reads these values
    before Phase 3**. They live in the template rather than in application
    configuration because they are only meaningful for the sheet design and
    print quality they were tuned against.

    Attributes:
        fill_ratio_threshold: Dark-pixel ratio at or above which a bubble counts
            as marked.
        blank_ratio_threshold: Dark-pixel ratio below which a bubble is
            certainly empty. Values between the two thresholds are ambiguous.
        ambiguity_margin: Minimum separation between the best and second best
            candidate in one group; a smaller gap raises a multiple-mark
            conflict for human review.
        min_confidence: Confidence below which a value is queued for human
            resolution instead of being accepted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fill_ratio_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    blank_ratio_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    ambiguity_margin: float = Field(default=0.12, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_threshold_order(self) -> RecognitionSettings:
        """Reject settings where the blank threshold is not below the fill threshold."""
        if self.blank_ratio_threshold >= self.fill_ratio_threshold:
            raise ValueError(
                "blank_ratio_threshold must be smaller than fill_ratio_threshold; "
                "otherwise no measurement could ever be ambiguous"
            )
        return self


class CalibrationRecord(BaseModel):
    """What the last completed Phase 4 calibration run found, if any.

    Declared here, next to :class:`RecognitionSettings`, because the two
    questions it answers are "were these thresholds actually checked against a
    real scan" and "against what geometry" - both properties of *this*
    template, not of the application. Nothing in Phase 1-3 reads this; only the
    calibration workflow writes it and only the Scan page's stale-validation
    warning reads it (``docs/calibration_workflow.md``).

    Attributes:
        validated_at: ISO-8601 UTC timestamp of the run that produced this
            record, or ``""`` when the template has never been calibrated -
            which is the default for every template loaded before Phase 4 and
            for one freshly created in the designer.
        sample_count: How many representative scans were tested.
        engine_version: Which :data:`~omr_scanner.services.recognition_models.ENGINE_VERSION`
            produced the run. A record from an older engine is not necessarily
            wrong, but it was not tested against *this* one.
        status: The calibration status the run concluded with (a
            :class:`~omr_scanner.services.calibration_service.CalibrationStatus`
            value, stored as plain text so this module never imports the
            services layer - see ``docs/ARCHITECTURE.md``).
        geometry_fingerprint: :meth:`OmrTemplate.geometry_fingerprint` at the
            time of the run.
        recognition_fingerprint: :meth:`OmrTemplate.recognition_fingerprint` at
            the time of the run.

    Why two fingerprints and not one:
        A geometry change (a moved zone, a resized bubble) and a settings
        change (a retuned threshold) invalidate a validation for different
        reasons, and a future message to the operator ("the template's
        geometry changed since this was validated" versus "the thresholds
        changed since this was validated") should be able to say which.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    validated_at: str = ""
    sample_count: int = Field(default=0, ge=0)
    engine_version: str = ""
    status: str = ""
    geometry_fingerprint: str = ""
    recognition_fingerprint: str = ""

    @property
    def is_recorded(self) -> bool:
        """Whether this template has ever been through calibration at all."""
        return bool(self.validated_at)


class BubbleOverride(BaseModel):
    """An explicit centre for one cell of a :class:`BubbleGrid`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: int = Field(ge=0)
    column: int = Field(ge=0)
    center: NormalizedPoint


class BubbleGrid(BaseModel):
    """Regular lattice of bubble centres inside a zone.

    A grid is stored instead of thousands of explicit coordinates because OMR
    sheets are printed on a regular pitch; storing the pitch keeps templates
    small, diffable and easy to nudge during calibration (Phase 4). Sheets with
    a genuinely irregular row can still be described, by overriding individual
    centres in :attr:`overrides`.

    Attributes:
        origin: Centre of the bubble at row 0, column 0.
        row_pitch: Normalised vertical distance between consecutive row centres.
        column_pitch: Normalised horizontal distance between consecutive column
            centres.
        bubble_size: Bounding size of one bubble, used to cut the measurement
            window in Phase 3.
        overrides: Explicit centres replacing the computed ones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: NormalizedPoint
    row_pitch: float = Field(ge=0.0, le=1.0)
    column_pitch: float = Field(ge=0.0, le=1.0)
    bubble_size: NormalizedSize
    overrides: tuple[BubbleOverride, ...] = ()

    def bubble_center(self, row: int, column: int) -> NormalizedPoint:
        """Return the centre of the bubble at ``(row, column)``.

        Assumes an axis-aligned lattice in the canonical frame; that holds
        because recognition only ever runs on a rectified sheet.

        Args:
            row: Zero-based row index, increasing downward.
            column: Zero-based column index, increasing rightward.

        Returns:
            The overridden centre when one is recorded for this cell, otherwise
            ``origin + (column * column_pitch, row * row_pitch)``.

        Raises:
            ValueError: ``row`` or ``column`` is negative.
            pydantic.ValidationError: The computed centre falls outside the page,
                which means the grid definition is inconsistent with the pitch.
        """
        if row < 0 or column < 0:
            raise ValueError("Bubble indices must be zero or positive")
        for override in self.overrides:
            if override.row == row and override.column == column:
                return override.center
        return NormalizedPoint(
            x=self.origin.x + column * self.column_pitch,
            y=self.origin.y + row * self.row_pitch,
        )


class GridFieldDefinition(BaseModel):
    """A character-per-column field: numeric, alphanumeric or set code.

    Attributes:
        type: One of ``numeric``, ``alphanumeric`` or ``set_code``.
        symbols: Permitted symbols, in printed order. For a roll number column
            this is ``("0", ..., "9")``.
        character_count: Number of character positions (for example 5 digits).
        symbol_axis: Whether the symbols run down the page or across it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[FieldType.NUMERIC, FieldType.ALPHANUMERIC, FieldType.SET_CODE]
    symbols: tuple[str, ...] = Field(min_length=2)
    character_count: int = Field(ge=1)
    symbol_axis: SymbolAxis = SymbolAxis.VERTICAL

    @field_validator("symbols")
    @classmethod
    def _unique_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate or empty symbols, which would make results ambiguous."""
        if any(symbol == "" for symbol in value):
            raise ValueError("Symbols must not be empty strings")
        if len(set(value)) != len(value):
            raise ValueError("Symbols must be unique within a field")
        return value

    @property
    def rows(self) -> int:
        """Number of bubble rows implied by the field definition."""
        if self.symbol_axis is SymbolAxis.VERTICAL:
            return len(self.symbols)
        return self.character_count

    @property
    def columns(self) -> int:
        """Number of bubble columns implied by the field definition."""
        if self.symbol_axis is SymbolAxis.VERTICAL:
            return self.character_count
        return len(self.symbols)


class QuestionBlockFieldDefinition(BaseModel):
    """A run of consecutive MCQ questions sharing one answer label set.

    Attributes:
        type: Always ``question_block``.
        first_question: Number printed next to the first question in the block
            (1-based, as the candidate sees it).
        question_count: How many consecutive questions the block contains.
        answer_labels: Option labels in printed order, e.g. ``("A","B","C","D")``.
        symbol_axis: ``HORIZONTAL`` when options run across the page (one row per
            question), ``VERTICAL`` when they run down it.
        group_id: Ties sibling columns generated together as one logical,
            multi-column Question Region, so the designer can offer
            group-wide operations (Distribute Columns, measuring the common
            column gap) without depending on zone-id naming conventions.
            ``None`` for a standalone single column - including every
            question-block column saved before this field existed, which
            loads unaffected and behaves exactly as it always did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[FieldType.QUESTION_BLOCK]
    first_question: int = Field(ge=1)
    question_count: int = Field(ge=1)
    answer_labels: tuple[str, ...] = Field(min_length=2)
    symbol_axis: SymbolAxis = SymbolAxis.HORIZONTAL
    group_id: str | None = None

    @field_validator("answer_labels")
    @classmethod
    def _unique_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate or empty answer labels."""
        if any(label == "" for label in value):
            raise ValueError("Answer labels must not be empty strings")
        if len(set(value)) != len(value):
            raise ValueError("Answer labels must be unique within a question block")
        return value

    @property
    def rows(self) -> int:
        """Number of bubble rows implied by the block layout."""
        if self.symbol_axis is SymbolAxis.HORIZONTAL:
            return self.question_count
        return len(self.answer_labels)

    @property
    def columns(self) -> int:
        """Number of bubble columns implied by the block layout."""
        if self.symbol_axis is SymbolAxis.HORIZONTAL:
            return len(self.answer_labels)
        return self.question_count

    @property
    def last_question(self) -> int:
        """Number of the final question in the block."""
        return self.first_question + self.question_count - 1


class IgnoredFieldDefinition(BaseModel):
    """A region excluded from recognition, kept so the designer can show it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[FieldType.IGNORED]


FieldDefinition = Annotated[
    GridFieldDefinition | QuestionBlockFieldDefinition | IgnoredFieldDefinition,
    Field(discriminator="type"),
]
"""Any field definition, discriminated by its ``type`` member."""


class Zone(BaseModel):
    """One rectangular region of the sheet with a logical meaning.

    Attributes:
        id: Stable identifier, unique within the template. Recognition results
            and conflicts reference zones by this id, so renaming the ``label``
            never breaks stored data.
        label: Human readable name shown in the designer and in conflict review.
        bounds: Region on the canonical page.
        field: What the region means.
        grid: Bubble lattice. Required for every field type except ``ignored``.
        display_color: ``#RRGGBB`` overlay colour used by the designer and by
            diagnostic images.
        recognition: Per-zone override of the template recognition settings;
            ``None`` means "inherit".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    label: str = Field(min_length=1)
    bounds: NormalizedRect
    field: FieldDefinition
    grid: BubbleGrid | None = None
    display_color: str = "#1E88E5"
    recognition: RecognitionSettings | None = None

    @field_validator("display_color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        """Require an explicit ``#RRGGBB`` colour; named colours are ambiguous."""
        if not _HEX_COLOR.match(value):
            raise ValueError("display_color must be a '#RRGGBB' hexadecimal colour")
        return value.upper()

    @model_validator(mode="after")
    def _check_geometry(self) -> Zone:
        """Validate the zone fits on the page and its bubbles fit in the zone.

        Failure modes caught here:
            * a zone dragged past the page edge in a future designer;
            * a grid whose pitch multiplied by the row/column count overflows the
              zone, which would silently measure the neighbouring field.
        """
        if not self.bounds.is_within_page():
            raise ValueError(f"Zone '{self.id}' extends beyond the page")

        if isinstance(self.field, IgnoredFieldDefinition):
            if self.grid is not None:
                raise ValueError(f"Ignored zone '{self.id}' must not define a bubble grid")
            return self

        if self.grid is None:
            raise ValueError(f"Zone '{self.id}' must define a bubble grid")

        last = self.grid.bubble_center(self.field.rows - 1, self.field.columns - 1)
        if not self.bounds.contains_point(last):
            raise ValueError(
                f"Zone '{self.id}': the bubble grid extends beyond the zone bounds "
                f"(last centre {last.x:.4f},{last.y:.4f})"
            )
        return self

    @property
    def bubble_count(self) -> int:
        """Total number of bubbles in the zone (zero for ignored regions)."""
        if isinstance(self.field, IgnoredFieldDefinition):
            return 0
        return self.field.rows * self.field.columns


class OmrTemplate(BaseModel):
    """A complete ``.omrt`` document.

    Attributes:
        format: Magic string; guards against opening unrelated JSON files.
        format_version: Document format version, see
            :data:`TEMPLATE_FORMAT_VERSION`.
        template_id: Stable identifier referenced by scans and results.
        name: Display name of the sheet design.
        description: Optional free text.
        created_with: OMRFlow version that wrote the document.
        created_at: Creation timestamp (UTC).
        modified_at: Last save timestamp (UTC).
        page: Canonical page geometry.
        registration_markers: Exactly four markers, one per corner role.
        orientation_marker: The marker that resolves 180 degree ambiguity.
        zones: Recognition zones, in designer order.
        recognition: Template-wide default recognition settings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["omrflow-template"] = TEMPLATE_FORMAT_ID
    format_version: int = TEMPLATE_FORMAT_VERSION
    template_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    description: str = ""
    created_with: str = __version__
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    modified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    page: PageGeometry
    registration_markers: tuple[RegistrationMarker, ...] = Field(min_length=4, max_length=4)
    orientation_marker: OrientationMarker
    zones: tuple[Zone, ...] = ()
    recognition: RecognitionSettings = RecognitionSettings()
    reference_image: str | None = Field(default=None)
    """Path to the reference sheet image the template was designed against,
    relative to the directory containing the ``.omrt`` file (the same
    relative-path convention a project uses for scans - see ADR-0002).

    Phase 2 (the template designer) writes this so a template can be reopened
    for further editing without asking the user to relocate the source image.
    It is never required: a template produced by hand, or one whose source
    image has since moved, simply has ``None`` here, and nothing downstream of
    Phase 1 alignment reads it. The image itself is never embedded - a `.omrt`
    document stays small, diffable JSON.

    Added in Phase 2 as an additive, optional field; per the versioning rule in
    ``docs/TEMPLATE_FORMAT.md`` this does not bump ``format_version``, and a
    document written before Phase 2 loads unchanged with this field ``None``.
    """

    default_bubble_radius: float | None = Field(default=None, gt=0.0, le=0.5)
    """Default bubble radius for regions generated in the designer, normalised to
    the **page width**.

    One number rather than a width and a height, because that is how a person
    describes a circular OMR bubble, and because two independent defaults are two
    things that can disagree. :meth:`default_bubble_size` derives the
    :class:`NormalizedSize` the grid model actually stores, using the page's own
    aspect ratio so that a bubble which is circular in *pixels* stays circular.

    Normalised to the width specifically (rather than to the shorter side or the
    diagonal) so that the horizontal half-axis is simply ``radius`` - the axis a
    user reads off a scan when measuring a printed bubble.

    A *region* may carry its own size: a region inherits this default for exactly
    as long as its stored bubble size matches what this radius produces (see
    :func:`~omr_scanner.domain.template_authoring.zone_inherits_bubble_size`).

    Additive and optional, like :attr:`reference_image` above and under the same
    versioning rule: ``None`` in a document written before this field existed,
    where :data:`~omr_scanner.domain.template_authoring.DEFAULT_BUBBLE_RADIUS`
    stands in - the same value every Phase 2 region dialog already hard-coded, so
    such a document's geometry is unaffected.
    """

    calibration: CalibrationRecord = CalibrationRecord()
    """The last Phase 4 calibration run against this template, if any.

    Additive, like :attr:`reference_image` and :attr:`default_bubble_radius`:
    a document written before Phase 4 loads with the default
    ``CalibrationRecord()``, whose :attr:`~CalibrationRecord.is_recorded` is
    ``False`` - "never calibrated", which is the truth for such a document,
    not an error.
    """

    @property
    def default_bubble_size(self) -> NormalizedSize:
        """The default bubble bounding size implied by :attr:`default_bubble_radius`.

        ``width = 2 * radius``; ``height`` is the same *physical* extent expressed
        on the vertical axis, which on a normalised page means multiplying by the
        page's aspect ratio. A 27 px radius on a 2480x3508 page therefore gives
        ``0.0218 x 0.0154`` - an ellipse in normalised coordinates describing a
        circle on paper.
        """
        radius = self.default_bubble_radius
        if radius is None:
            radius = _FALLBACK_DEFAULT_BUBBLE_RADIUS
        return NormalizedSize(
            width=min(2.0 * radius, 1.0),
            height=min(2.0 * radius * self.page.aspect_ratio, 1.0),
        )

    @field_validator("registration_markers")
    @classmethod
    def _distinct_roles(
        cls, value: tuple[RegistrationMarker, ...]
    ) -> tuple[RegistrationMarker, ...]:
        """Require one marker per corner; ordering in the file is irrelevant."""
        roles = {marker.role for marker in value}
        missing = set(MarkerRole) - roles
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"Missing registration marker(s) for: {names}")
        return value

    @field_validator("zones")
    @classmethod
    def _unique_zone_ids(cls, value: tuple[Zone, ...]) -> tuple[Zone, ...]:
        """Reject duplicate zone ids, which would make results unattributable."""
        seen: set[str] = set()
        for zone in value:
            if zone.id in seen:
                raise ValueError(f"Duplicate zone id: '{zone.id}'")
            seen.add(zone.id)
        return value

    def zone_by_id(self, zone_id: str) -> Zone | None:
        """Return the zone with ``zone_id``, or ``None`` when absent."""
        return next((zone for zone in self.zones if zone.id == zone_id), None)

    def marker_by_role(self, role: MarkerRole) -> RegistrationMarker:
        """Return the registration marker for ``role``.

        Raises:
            KeyError: No marker carries that role. Validation makes this
                unreachable for a validated document.
        """
        for marker in self.registration_markers:
            if marker.role is role:
                return marker
        raise KeyError(f"No registration marker with role {role.value}")

    def effective_recognition(self, zone: Zone) -> RecognitionSettings:
        """Return the recognition settings that apply to ``zone``."""
        return zone.recognition if zone.recognition is not None else self.recognition

    # ------------------------------------------------------------------
    # Calibration fingerprints (Phase 4)
    # ------------------------------------------------------------------
    def geometry_fingerprint(self) -> str:
        """Return a stable hash of everything a bubble's position depends on.

        Covers :attr:`page`, :attr:`registration_markers`, :attr:`orientation_marker`
        and :attr:`zones` - nothing else. Deliberately excludes ``name``,
        ``description``, ``created_at``/``modified_at`` and :attr:`recognition`
        (template-level *and* per-zone), so that renaming a template, or
        retuning a threshold, does not by itself invalidate a calibration that
        never looked at geometry in the first place - see
        :meth:`recognition_fingerprint` for the settings half of that
        distinction.

        A change here means every bubble sampling window Phase 4 showed the
        operator may now be wrong, which is exactly the condition
        :meth:`is_calibration_current` exists to catch (``docs/calibration_workflow.md``).
        """
        payload = {
            "page": self.page.model_dump(mode="json"),
            "registration_markers": [
                marker.model_dump(mode="json") for marker in self.registration_markers
            ],
            "orientation_marker": self.orientation_marker.model_dump(mode="json"),
            "zones": [
                zone.model_dump(mode="json", exclude={"recognition"})
                for zone in self.zones
            ],
        }
        return _stable_hash(payload)

    def recognition_fingerprint(self) -> str:
        """Return a stable hash of every recognition threshold in effect.

        Covers the template-level :attr:`recognition` and every zone's own
        override, so a calibration run is invalidated the moment *any*
        threshold a sheet is actually read with changes - including one set on
        a single zone, which :meth:`geometry_fingerprint` does not see.
        """
        payload = {
            "recognition": self.recognition.model_dump(mode="json"),
            "zone_overrides": {
                zone.id: zone.recognition.model_dump(mode="json")
                for zone in self.zones
                if zone.recognition is not None
            },
        }
        return _stable_hash(payload)

    def is_calibration_current(self) -> bool:
        """Whether :attr:`calibration` still describes this template.

        ``False`` for a template that has never been calibrated at all - see
        :attr:`CalibrationRecord.is_recorded` - and for one whose geometry or
        recognition settings have moved since the recorded run.
        """
        record = self.calibration
        if not record.is_recorded:
            return False
        return (
            record.geometry_fingerprint == self.geometry_fingerprint()
            and record.recognition_fingerprint == self.recognition_fingerprint()
        )

    def with_calibration(self, record: CalibrationRecord) -> OmrTemplate:
        """Return a copy of this template carrying a new calibration record.

        A plain, documented use of ``model_copy`` - the same mechanism the
        template designer already uses for every edit - so that recording a
        calibration result never touches anything else about the document.
        """
        return self.model_copy(update={"calibration": record})


def _stable_hash(payload: object) -> str:
    """Return a short, deterministic hash of a JSON-safe structure.

    ``json.dumps(..., sort_keys=True)`` makes dictionary key order irrelevant,
    which matters here because Pydantic's own ``model_dump`` order is an
    implementation detail, not part of the format - two structurally identical
    templates must fingerprint the same regardless of how their fields happen
    to have been declared. SHA-256 rather than Python's built-in ``hash()``
    because the latter is salted per process and would make a fingerprint
    written by one run unrecognisable to the next.

    Sixteen hex characters (64 bits) is not cryptographic - nothing here is a
    security boundary - only enough to make an accidental collision between
    two different templates practically impossible.
    """
    import json

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]
