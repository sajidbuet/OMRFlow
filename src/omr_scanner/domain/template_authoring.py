"""Pure geometry for turning a designer's high-level intent into template zones.

Purpose:
    Bridge the gap between what a template-designer *user* specifies ("7 digit
    student ID", "questions 1-100 in 4 columns of A/B/C/D") and what the
    ``.omrt`` format actually stores (a :class:`~omr_scanner.domain.template.Zone`
    with an explicit :class:`~omr_scanner.domain.template.BubbleGrid` pitch).

Responsibilities:
    * Fit a regular bubble grid inside a rectangle the user drew, computing the
      pitch so the first and last bubble sit symmetrically inside it.
    * Generate the four region kinds the Phase 2 brief describes (student ID,
      question set, question-answer columns, custom bubble group) as ordinary
      :class:`Zone` objects - no new domain types are introduced, because the
      existing field/grid model already stores "the location of every expected
      bubble" rather than one rectangle per region.
    * Layer designer-facing validation (overlaps, question-number gaps and
      duplicates) on top of what :class:`~omr_scanner.domain.template.OmrTemplate`
      already enforces at construction time.

What does NOT belong here:
    * Any Qt or file I/O. This module is exercised the same way the rest of
      ``domain`` is: with plain values, no display required.
    * Deciding *where* on the canvas a region goes - that is the GUI's job; this
      module only turns a chosen rectangle and a chosen count/label scheme into
      valid geometry.

Design note - one zone per question column:
    ``docs/TEMPLATE_FORMAT.md`` already specifies that a long question block is
    described as several zones, one per printed column, because a single
    :class:`~omr_scanner.domain.template.BubbleGrid` can only express one pitch
    pair. :func:`generate_question_columns` therefore returns a tuple of zones,
    never a single one, even when ``columns == 1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    _FALLBACK_DEFAULT_BUBBLE_RADIUS,
    BubbleGrid,
    BubbleOverride,
    FieldType,
    GridFieldDefinition,
    IgnoredFieldDefinition,
    MarkerRole,
    OmrTemplate,
    OrientationMarker,
    PageGeometry,
    QuestionBlockFieldDefinition,
    RegistrationMarker,
    SymbolAxis,
    Zone,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

DEFAULT_DISPLAY_COLORS: dict[FieldType, str] = {
    FieldType.NUMERIC: "#1E88E5",
    FieldType.ALPHANUMERIC: "#6D4C41",
    FieldType.SET_CODE: "#8E24AA",
    FieldType.QUESTION_BLOCK: "#2E7D32",
    FieldType.IGNORED: "#757575",
}
"""One default colour per field kind, so a freshly generated region is never
the same indistinguishable blue as everything else on the canvas."""

_MIN_BUBBLES_PER_AXIS = 1
"""A grid must have at least one row and one column to mean anything."""

_GRID_FIT_TOLERANCE = 1e-9
"""Slack allowed when checking that a lattice fits its rectangle. A pitch that
was itself derived from that rectangle comes back a few ULPs over after the
round trip; without this, feeding an auto-fitted pitch straight back in would be
rejected as not fitting the rectangle it was measured from."""

DEFAULT_MARKER_INSET = 0.05
"""Default normalised distance from each page edge to a corner marker's centre,
used only to seed a brand new template. Kept as a local literal rather than
importing :data:`omr_scanner.imaging.config.DEFAULT_MARKER_TARGETS` - which
uses the same value - because ``domain`` must never import ``imaging``
(``docs/ARCHITECTURE.md``); the two are independent defaults that merely agree
by convention today."""

DEFAULT_MARKER_SIZE = NormalizedSize(width=0.03, height=0.021)
"""Default printed marker size for a brand new template."""

DEFAULT_ORIENTATION_CENTER = NormalizedPoint(x=0.14, y=0.035)
"""Default orientation-mark centre for a brand new template."""

DEFAULT_ORIENTATION_SIZE = NormalizedSize(width=0.05, height=0.012)
"""Default orientation-mark size for a brand new template."""

DEFAULT_BUBBLE_RADIUS_PX = 20.0
"""Bubble radius a **brand new** template starts with, in canonical page pixels.

Stated in pixels because that is the unit the designer shows and the unit a
person measuring a printed sheet with a ruler is working in; the model stores
it normalised, and :func:`default_bubble_radius_for` does the conversion using
the page the template is actually being built for. A single normalised constant
could not mean "20 px" on two pages of different resolutions.

Deliberately *not* the same number as
:data:`~omr_scanner.domain.template._FALLBACK_DEFAULT_BUBBLE_RADIUS`, which the
model uses for documents saved before ``default_bubble_radius`` existed. That
one must never move: it is what those documents' geometry means, and changing
it would silently resize every bubble in every template that predates the
field. New templates get this; old templates keep theirs."""


def default_bubble_radius_for(canonical_width_px: int) -> float:
    """The new-template default radius, normalised to a page this wide.

    Args:
        canonical_width_px: The template's canonical page width in pixels.

    Returns:
        :data:`DEFAULT_BUBBLE_RADIUS_PX` expressed as a fraction of the page
        width, clamped to the range the model accepts.
    """
    if canonical_width_px <= 0:
        return _FALLBACK_DEFAULT_BUBBLE_RADIUS
    return min(0.5, DEFAULT_BUBBLE_RADIUS_PX / float(canonical_width_px))


DEFAULT_BUBBLE_RADIUS = _FALLBACK_DEFAULT_BUBBLE_RADIUS
"""The radius assumed for a template that does not state one.

Normalised to the **page width** - so a radius is one number, the way a user
thinks about a circular OMR bubble, rather than an independent width and
height. See :meth:`~omr_scanner.domain.template.OmrTemplate.default_bubble_size`
for how the vertical half-axis is derived from the page aspect ratio, which is
what keeps a bubble that is circular *in pixels* circular.

Half the ``0.022`` normalised bubble width every Phase 2 region dialog
hard-coded, so a document from before ``default_bubble_radius`` existed reads
back with the geometry it was drawn with. A *new* template does not use this -
see :data:`DEFAULT_BUBBLE_RADIUS_PX`."""


class ColumnLayoutMode(StrEnum):
    """How :func:`generate_question_columns` decides each column strip's size.

    The distinction exists because two different user gestures both produce
    question columns and they want opposite things from the rectangle they are
    given:

    * The designer's "draw a Question Region, then configure it" flow hands over
      a rectangle the user physically selected on the sheet. That rectangle is a
      *container*: changing the column count, the questions per column, the
      answer-choice count, the column gap or the bubble size must reflow what is
      inside it and must never move or resize it.
    * "Create Column Array" hands over one already-calibrated column and asks for
      N more like it. There the reference column's own pitch defines the size and
      the array is *expected* to extend past the reference rectangle.

    Leaving this implicit is what let a container-shaped call silently behave like
    an array-shaped one (see ``docs/development/template_gui_fix_diagnosis.md``
    §1).
    """

    FIT_CONTAINER = "fit_container"
    """``bounds`` is the user's container and is preserved exactly. The strips
    tile it: ``strip_width = (bounds.width - column_gap * (columns - 1)) /
    columns`` and ``strip_height = bounds.height``. An explicit pitch, if given,
    positions the lattice *inside* its strip and must fit."""

    FROM_PITCH = "from_pitch"
    """``bounds`` anchors the top-left corner only; each strip's size is derived
    from ``row_pitch``/``column_pitch`` and the bubble size, so the block's outer
    extent grows with the column count. Requires both pitches."""


def build_blank_template(
    *,
    name: str,
    canonical_width_px: int,
    canonical_height_px: int,
    width_mm: float = 210.0,
    height_mm: float = 297.0,
) -> OmrTemplate:
    """Construct a new template with placeholder markers and no regions.

    ``OmrTemplate`` requires exactly four registration markers and an
    orientation marker to exist at all - there is no "markers not yet decided"
    state in the persisted format - so a brand new template starts with them
    at reasonable default positions. The designer marks these as *unconfirmed*
    session state (see
    :class:`~omr_scanner.gui.template_designer.state.MarkerStatus`) so the
    canvas draws them as needing attention until the user runs detection or
    drags them into place.

    Args:
        name: Template display name.
        canonical_width_px: Canonical page width - typically the reference
            image's own width, since the designer works directly on it.
        canonical_height_px: Canonical page height.
        width_mm: Physical page width, for reference; A4 by default.
        height_mm: Physical page height; A4 by default.

    Returns:
        A valid, saveable (if incomplete-looking) template.
    """
    inset = DEFAULT_MARKER_INSET
    corner_positions = {
        MarkerRole.TOP_LEFT: (inset, inset),
        MarkerRole.TOP_RIGHT: (1.0 - inset, inset),
        MarkerRole.BOTTOM_RIGHT: (1.0 - inset, 1.0 - inset),
        MarkerRole.BOTTOM_LEFT: (inset, 1.0 - inset),
    }
    markers = tuple(
        RegistrationMarker(
            role=role, center=NormalizedPoint(x=x, y=y), size=DEFAULT_MARKER_SIZE
        )
        for role, (x, y) in corner_positions.items()
    )
    orientation = OrientationMarker(
        center=DEFAULT_ORIENTATION_CENTER,
        size=DEFAULT_ORIENTATION_SIZE,
        expected_near=MarkerRole.TOP_LEFT,
    )
    return OmrTemplate(
        name=name,
        page=PageGeometry(
            width_mm=width_mm,
            height_mm=height_mm,
            canonical_width_px=canonical_width_px,
            canonical_height_px=canonical_height_px,
        ),
        registration_markers=markers,
        orientation_marker=orientation,
        default_bubble_radius=default_bubble_radius_for(canonical_width_px),
    )


def fit_grid_to_bounds(
    *, bounds: NormalizedRect, rows: int, columns: int, bubble_size: NormalizedSize
) -> BubbleGrid:
    """Compute a pitch that spreads ``rows`` x ``columns`` bubbles evenly in ``bounds``.

    The first bubble is centred half a bubble-size in from the top-left corner
    of ``bounds`` and the last is the same distance in from the bottom-right, so
    the grid reads as evenly inset rather than flush against the region's edge.
    With only one row (or column), that axis's pitch is zero and every bubble
    on it shares the same coordinate - centred in the bounds.

    Args:
        bounds: The region the grid must fit inside. Every bubble centre this
            function computes lies within it, by construction.
        rows: Number of bubble rows, at least 1.
        columns: Number of bubble columns, at least 1.
        bubble_size: Bounding size of one bubble.

    Returns:
        A grid with no overrides.

    Raises:
        ValueError: ``rows`` or ``columns`` is less than 1, or ``bounds`` is too
            small to hold even one bubble at the requested size.
    """
    if rows < _MIN_BUBBLES_PER_AXIS or columns < _MIN_BUBBLES_PER_AXIS:
        raise ValueError("A grid needs at least one row and one column")

    half_width = bubble_size.width / 2.0
    half_height = bubble_size.height / 2.0
    if bubble_size.width > bounds.width or bubble_size.height > bounds.height:
        raise ValueError(
            f"A {bubble_size.width:.4f}x{bubble_size.height:.4f} bubble does not fit "
            f"inside a {bounds.width:.4f}x{bounds.height:.4f} region"
        )

    origin_x = bounds.x + half_width
    origin_y = bounds.y + half_height
    column_pitch = (
        (bounds.width - bubble_size.width) / (columns - 1) if columns > 1 else 0.0
    )
    row_pitch = (bounds.height - bubble_size.height) / (rows - 1) if rows > 1 else 0.0

    # A single-axis grid is centred rather than pinned to the top-left corner,
    # which is what "one row" (a set-code strip) or "one column" (a single
    # digit) should look like on the page.
    if columns == 1:
        origin_x = bounds.center.x
    if rows == 1:
        origin_y = bounds.center.y

    return BubbleGrid(
        origin=NormalizedPoint(x=origin_x, y=origin_y),
        row_pitch=row_pitch,
        column_pitch=column_pitch,
        bubble_size=bubble_size,
    )


def place_grid_in_bounds(
    *,
    bounds: NormalizedRect,
    rows: int,
    columns: int,
    bubble_size: NormalizedSize,
    row_pitch: float,
    column_pitch: float,
) -> BubbleGrid:
    """Place a grid of a **given** pitch inside ``bounds`` without resizing anything.

    The counterpart to :func:`fit_grid_to_bounds`: that one derives the pitch from
    the rectangle, this one takes the pitch as given and only decides where the
    lattice sits. Together they are what lets a user-drawn container rectangle stay
    fixed while its internal layout is re-generated (see :class:`ColumnLayoutMode`).

    The lattice is anchored to the top-left of ``bounds``, half a bubble in, so
    that changing the pitch keeps the first row and first column exactly where they
    were - the behaviour that makes calibrating against a printed sheet
    predictable. An axis holding a single bubble is centred instead, matching
    :func:`fit_grid_to_bounds` exactly, so a caller that passes the auto-fitted
    pitch back in gets the auto-fitted grid back out.

    Args:
        bounds: The rectangle the lattice must fit inside. Never modified - it is
            the caller's fixed container.
        rows: Number of bubble rows, at least 1.
        columns: Number of bubble columns, at least 1.
        bubble_size: Bounding size of one bubble.
        row_pitch: Normalised vertical centre-to-centre distance.
        column_pitch: Normalised horizontal centre-to-centre distance.

    Returns:
        A grid with no overrides, every centre inside ``bounds``.

    Raises:
        ValueError: ``rows``/``columns`` is less than 1, a pitch is negative, a
            single bubble does not fit in ``bounds``, or the lattice at this pitch
            would extend past ``bounds`` - which is the caller asking for a layout
            that does not fit the region the user selected, and is reported rather
            than silently enlarging the region.
    """
    if rows < _MIN_BUBBLES_PER_AXIS or columns < _MIN_BUBBLES_PER_AXIS:
        raise ValueError("A grid needs at least one row and one column")
    if row_pitch < 0.0 or column_pitch < 0.0:
        raise ValueError("Bubble pitch must not be negative")
    if bubble_size.width > bounds.width or bubble_size.height > bounds.height:
        raise ValueError(
            f"A {bubble_size.width:.4f}x{bubble_size.height:.4f} bubble does not fit "
            f"inside a {bounds.width:.4f}x{bounds.height:.4f} region"
        )

    span_width = bubble_size.width + (columns - 1) * column_pitch
    span_height = bubble_size.height + (rows - 1) * row_pitch
    if span_width > bounds.width + _GRID_FIT_TOLERANCE:
        raise ValueError(
            f"{columns} bubbles at a {column_pitch:.4f} pitch span {span_width:.4f}, "
            f"wider than the {bounds.width:.4f} region. Reduce the spacing, the "
            f"bubble size or the number of columns, or draw a wider region."
        )
    if span_height > bounds.height + _GRID_FIT_TOLERANCE:
        raise ValueError(
            f"{rows} bubbles at a {row_pitch:.4f} pitch span {span_height:.4f}, "
            f"taller than the {bounds.height:.4f} region. Reduce the spacing, the "
            f"bubble size or the number of rows, or draw a taller region."
        )

    origin_x = (
        bounds.center.x if columns == 1 else bounds.x + bubble_size.width / 2.0
    )
    origin_y = bounds.center.y if rows == 1 else bounds.y + bubble_size.height / 2.0
    return BubbleGrid(
        origin=NormalizedPoint(x=origin_x, y=origin_y),
        row_pitch=row_pitch,
        column_pitch=column_pitch,
        bubble_size=bubble_size,
    )


def generate_character_grid_zone(
    *,
    zone_id: str,
    label: str,
    field_type: FieldType,
    symbols: Sequence[str],
    character_count: int,
    bounds: NormalizedRect,
    bubble_size: NormalizedSize,
    symbol_axis: SymbolAxis = SymbolAxis.VERTICAL,
    display_color: str | None = None,
) -> Zone:
    """Build a Student ID, Question Set or custom character-per-column region.

    Args:
        zone_id: Stable identifier for the new zone.
        label: Human readable name.
        field_type: One of ``NUMERIC``, ``ALPHANUMERIC`` or ``SET_CODE``. A
            ``QUESTION_BLOCK`` or ``IGNORED`` region is built by the dedicated
            functions below instead.
        symbols: Permitted symbols in printed order (e.g. ``"0".."9"``).
        character_count: Number of character positions.
        bounds: Region on the canonical page the bubble grid is fitted into.
        bubble_size: Bounding size of one bubble.
        symbol_axis: Whether symbols run down the page or across it.
        display_color: Overlay colour; a sensible per-field-kind default is
            used when omitted.

    Returns:
        A fully valid :class:`Zone`.

    Raises:
        ValueError: ``field_type`` is not a character-grid field kind.
    """
    if field_type not in (FieldType.NUMERIC, FieldType.ALPHANUMERIC, FieldType.SET_CODE):
        raise ValueError(f"{field_type} is not a character-grid field kind")

    field = GridFieldDefinition(
        type=field_type,
        symbols=tuple(symbols),
        character_count=character_count,
        symbol_axis=symbol_axis,
    )
    grid = fit_grid_to_bounds(
        bounds=bounds, rows=field.rows, columns=field.columns, bubble_size=bubble_size
    )
    return Zone(
        id=zone_id,
        label=label,
        bounds=bounds,
        field=field,
        grid=grid,
        display_color=display_color or DEFAULT_DISPLAY_COLORS[field_type],
    )


def generate_question_columns(
    *,
    id_prefix: str,
    label_prefix: str,
    first_question: int,
    question_count: int,
    answer_labels: Sequence[str],
    columns: int,
    questions_per_column: int,
    bounds: NormalizedRect,
    bubble_size: NormalizedSize,
    symbol_axis: SymbolAxis = SymbolAxis.HORIZONTAL,
    column_gap: float = 0.0,
    row_pitch: float | None = None,
    column_pitch: float | None = None,
    layout_mode: ColumnLayoutMode = ColumnLayoutMode.FIT_CONTAINER,
    display_color: str | None = None,
    group_id: str | None = None,
) -> tuple[Zone, ...]:
    """Build one :class:`Zone` per printed column of a question-answer block.

    Splitting the block into ``columns`` strips (separated by ``column_gap``)
    and generating one zone per strip is what lets the persistent template
    describe every one of, say, 400 bubbles individually rather than as a
    single rectangle a recognition pass would have to subdivide by guesswork
    later.

    ``layout_mode`` decides what ``bounds`` means; see :class:`ColumnLayoutMode`.

    Under :attr:`ColumnLayoutMode.FIT_CONTAINER` (the default), ``bounds`` is the
    container and is preserved exactly:

    * ``strip_width = (bounds.width - column_gap * (columns - 1)) / columns``,
      ``strip_height = bounds.height``. The union of the returned zones' bounds
      therefore reproduces ``bounds`` for **any** column count, gap, pitch or
      bubble size - which is the invariant that keeps a user-drawn Question
      Region rectangle where the user drew it.
    * ``row_pitch``/``column_pitch`` omitted: each strip's pitch is auto-fitted
      to the strip via :func:`fit_grid_to_bounds` - the original Phase 2
      behaviour, byte-for-byte.
    * ``row_pitch``/``column_pitch`` given (normalised, matching
      :class:`~omr_scanner.domain.template.BubbleGrid`'s own field names - not
      axis-aware "choice spacing"/"row spacing" labels, which is the caller's job
      to map, exactly as :attr:`QuestionBlockFieldDefinition.rows`/``.columns``
      already do): the lattice is *placed inside* the strip at that pitch, top-left
      anchored, with a single-bubble axis centred exactly as
      :func:`fit_grid_to_bounds` centres one. A pitch too large for the strip is
      rejected rather than silently enlarging the region.

    Under :attr:`ColumnLayoutMode.FROM_PITCH`, ``bounds`` anchors the top-left
    corner only and each strip's size is *derived* from the requested pitch,
    ``bubble_size`` and a full (``questions_per_column``-sized) column, so a real
    column with the full question count reproduces the requested pitch exactly; a
    shorter remainder column (when ``question_count`` does not divide evenly by
    ``questions_per_column``) is fitted into the same, uniformly-sized strip.

    Column placement uses one formula regardless of sizing mode - the
    "empty space between adjacent bounding boxes" convention
    ``docs/TEMPLATE_FORMAT.md`` documents for ``column_gap``:

    ```
    next_column_x = current_column_x + current_column_width + column_gap
    ```

    Args:
        id_prefix: Zone ids are ``f"{id_prefix}_{n}"`` for column index ``n``
            (0-based), guaranteed unique among the returned zones.
        label_prefix: Human readable label prefix, e.g. ``"Questions"`` becomes
            ``"Questions 1-25"``.
        first_question: Number of the first question overall (1-based).
        question_count: Total questions across every column.
        answer_labels: Option labels in printed order, e.g. ``("A","B","C","D")``.
        columns: Number of printed columns.
        questions_per_column: How many questions each column holds, except
            possibly the last, which holds the remainder.
        bounds: The container under
            :attr:`ColumnLayoutMode.FIT_CONTAINER`; the top-left anchor only
            under :attr:`ColumnLayoutMode.FROM_PITCH`.
        bubble_size: Bounding size of one bubble.
        symbol_axis: ``HORIZONTAL`` (options run across, one row per question)
            or ``VERTICAL``.
        column_gap: Empty normalised horizontal gap between adjacent column
            bounding boxes.
        row_pitch: Explicit normalised vertical bubble pitch; must be given
            together with ``column_pitch`` or not at all.
        column_pitch: Explicit normalised horizontal bubble pitch.
        layout_mode: What ``bounds`` means; see :class:`ColumnLayoutMode`.
        display_color: Overlay colour; defaults to the question-block colour.
        group_id: Shared identifier recorded on every returned zone's
            :attr:`QuestionBlockFieldDefinition.group_id`. Defaults to
            ``id_prefix`` - one call to this function is always "one logical
            Question Region", so reusing the already-unique id prefix needs no
            separate identifier scheme.

    Returns:
        One zone per column that actually holds at least one question. A
        block of 100 questions in columns of 25 with ``columns=5`` therefore
        returns 4 zones, not 5, because the fifth would be empty.

    Raises:
        ValueError: ``columns`` or ``questions_per_column`` is less than 1,
            exactly one of ``row_pitch``/``column_pitch`` is given,
            :attr:`ColumnLayoutMode.FROM_PITCH` was requested without a pitch,
            the resulting strip width leaves no room for the columns, or an
            explicit pitch does not fit the container's strips.
    """
    if columns < 1:
        raise ValueError("columns must be at least 1")
    if questions_per_column < 1:
        raise ValueError("questions_per_column must be at least 1")
    if (row_pitch is None) != (column_pitch is None):
        raise ValueError("row_pitch and column_pitch must both be given, or neither")

    nominal_field = QuestionBlockFieldDefinition(
        type=FieldType.QUESTION_BLOCK,
        first_question=1,
        question_count=questions_per_column,
        answer_labels=tuple(answer_labels),
        symbol_axis=symbol_axis,
    )
    if layout_mode is ColumnLayoutMode.FROM_PITCH:
        if row_pitch is None or column_pitch is None:
            raise ValueError(
                "ColumnLayoutMode.FROM_PITCH derives each column's size from the "
                "pitch, so row_pitch and column_pitch are both required"
            )
        strip_width = bubble_size.width + max(nominal_field.columns - 1, 0) * column_pitch
        strip_height = bubble_size.height + max(nominal_field.rows - 1, 0) * row_pitch
    else:
        strip_width = (bounds.width - column_gap * (columns - 1)) / columns
        strip_height = bounds.height
    if strip_width <= 0.0:
        raise ValueError("column_gap leaves no room for the question columns")

    color = display_color or DEFAULT_DISPLAY_COLORS[FieldType.QUESTION_BLOCK]
    resolved_group_id = group_id if group_id is not None else id_prefix
    zones: list[Zone] = []
    remaining = question_count
    question_cursor = first_question

    for column_index in range(columns):
        if remaining <= 0:
            break
        count_here = min(questions_per_column, remaining)
        strip_x = bounds.x + column_index * (strip_width + column_gap)
        strip_bounds = NormalizedRect(
            x=strip_x, y=bounds.y, width=strip_width, height=strip_height
        )

        field = QuestionBlockFieldDefinition(
            type=FieldType.QUESTION_BLOCK,
            first_question=question_cursor,
            question_count=count_here,
            answer_labels=tuple(answer_labels),
            symbol_axis=symbol_axis,
            group_id=resolved_group_id,
        )
        if layout_mode is ColumnLayoutMode.FIT_CONTAINER and row_pitch is not None:
            # An explicit pitch positions the lattice inside the container's
            # strip; it never resizes the strip. `column_pitch` is non-None
            # whenever `row_pitch` is - the paired check above guarantees it.
            assert column_pitch is not None
            grid = place_grid_in_bounds(
                bounds=strip_bounds,
                rows=field.rows,
                columns=field.columns,
                bubble_size=bubble_size,
                row_pitch=row_pitch,
                column_pitch=column_pitch,
            )
        else:
            grid = fit_grid_to_bounds(
                bounds=strip_bounds,
                rows=field.rows,
                columns=field.columns,
                bubble_size=bubble_size,
            )
        zones.append(
            Zone(
                id=f"{id_prefix}_{column_index}",
                label=f"{label_prefix} {question_cursor}-{field.last_question}",
                bounds=strip_bounds,
                field=field,
                grid=grid,
                display_color=color,
            )
        )

        question_cursor += count_here
        remaining -= count_here

    return tuple(zones)


def generate_column_array(
    reference: Zone,
    *,
    columns: int,
    questions_per_column: int,
    first_question: int,
    gap: float,
    direction: Literal["left_to_right", "right_to_left"] = "left_to_right",
    id_prefix: str | None = None,
) -> tuple[Zone, ...]:
    """Generate a full set of question columns from one calibrated reference.

    Reads bubble size, pitch, answer labels and layout straight off
    ``reference`` (so every generated sibling is visually identical to the
    column the user already positioned by hand) and delegates the actual
    layout to :func:`generate_question_columns` in its explicit-pitch mode,
    anchored so that ``reference``'s own top-left corner is preserved as one
    of the generated columns' positions.

    Args:
        reference: An existing, calibrated question-block zone to array from.
        columns: Total number of columns to produce (including one at the
            reference's own position).
        questions_per_column: Questions each generated column holds.
        first_question: Number printed beside the first question overall.
        gap: Empty normalised horizontal gap between adjacent columns - the
            same convention as :func:`generate_question_columns`'s
            ``column_gap``.
        direction: ``"left_to_right"`` extends the array to the right of
            ``reference``'s position (which becomes the leftmost column);
            ``"right_to_left"`` extends it to the left (``reference``'s
            position becomes the rightmost column).
        id_prefix: Overrides the generated zones' id prefix (default:
            ``reference.id``) - needed when ``reference.id`` itself would
            collide with another zone already in the template once the
            reference zone is replaced by this array.

    Returns:
        ``columns`` zones, ids derived from ``id_prefix`` (default
        ``reference.id``), sharing one fresh ``group_id``. Bubble overrides on
        ``reference`` (fine-tuned positions) are carried into every generated
        column, since they are part of the calibrated geometry a real,
        imperfectly-printed sheet's repeated columns are expected to share.

    Raises:
        ValueError: ``reference`` is not a question-answer block, has no
            bubble grid, or ``columns``/``questions_per_column`` is less
            than 1.
    """
    if not isinstance(reference.field, QuestionBlockFieldDefinition):
        raise ValueError("The reference region must be a question-answer block")
    if reference.grid is None:
        raise ValueError("The reference region has no bubble grid")
    if columns < 1:
        raise ValueError("columns must be at least 1")
    if questions_per_column < 1:
        raise ValueError("questions_per_column must be at least 1")

    nominal_field = QuestionBlockFieldDefinition(
        type=FieldType.QUESTION_BLOCK,
        first_question=1,
        question_count=questions_per_column,
        answer_labels=reference.field.answer_labels,
        symbol_axis=reference.field.symbol_axis,
    )
    strip_width = (
        reference.grid.bubble_size.width
        + max(nominal_field.columns - 1, 0) * reference.grid.column_pitch
    )
    if direction == "left_to_right":
        origin_x = reference.bounds.x
    else:
        origin_x = reference.bounds.x - (columns - 1) * (strip_width + gap)

    anchor_bounds = NormalizedRect(
        x=origin_x,
        y=reference.bounds.y,
        width=reference.bounds.width,
        height=reference.bounds.height,
    )
    return generate_question_columns(
        id_prefix=id_prefix if id_prefix is not None else reference.id,
        label_prefix="Questions",
        first_question=first_question,
        question_count=columns * questions_per_column,
        answer_labels=reference.field.answer_labels,
        columns=columns,
        questions_per_column=questions_per_column,
        bounds=anchor_bounds,
        bubble_size=reference.grid.bubble_size,
        symbol_axis=reference.field.symbol_axis,
        column_gap=gap,
        row_pitch=reference.grid.row_pitch,
        column_pitch=reference.grid.column_pitch,
        # "Array from this calibrated column" is the one gesture where growing
        # past the reference rectangle is the whole point: every generated
        # sibling must be the same size as the column the user positioned, not a
        # slice of it. See `ColumnLayoutMode`.
        layout_mode=ColumnLayoutMode.FROM_PITCH,
        display_color=reference.display_color,
    )


@dataclass(frozen=True, slots=True)
class ColumnSpacingInfo:
    """Whether a set of question columns are evenly spaced, and by how much.

    Attributes:
        uniform: Whether every gap between adjacent column bounding boxes
            matches (within a small floating-point tolerance).
        column_gap: The common normalised gap, when ``uniform`` is true;
            ``None`` otherwise (including when there are fewer than two
            columns to compare).
    """

    uniform: bool
    column_gap: float | None = None


_COLUMN_GAP_TOLERANCE = 1e-4

BUBBLE_SIZE_RELATIVE_TOLERANCE = 0.02
"""How close a zone's bubble size must be to the template default, relative to
that default, to count as inheriting it.

*Relative*, and this loose, because the size makes a round trip the designer
cannot avoid: normalised in the document -> pixels in a one-decimal spin box ->
normalised again. A ``0.011`` default on a 620 px-wide sheet is ``6.82 px``,
displays as ``6.8``, and comes back as ``0.010968`` - a 0.3% difference that
exact equality would read as "the user chose their own radius", silently
detaching every region from the template default.

Two per cent is far below any deliberate override: the smallest change a person
can make with this spin box is 0.1 px on a single-digit radius, well over 1%,
and a realistic adjustment is a whole pixel or more."""


def measure_column_gap(
    columns: Sequence[Zone], *, tolerance: float = _COLUMN_GAP_TOLERANCE
) -> ColumnSpacingInfo:
    """Measure whether ``columns`` are evenly spaced, per the ``column_gap`` convention above.

    Args:
        columns: The zones to measure (order does not matter - sorted here by
            their own ``bounds.x``).
        tolerance: Maximum normalised difference between the largest and
            smallest gap still considered "uniform".

    Returns:
        A report naming the common gap when uniform, or flagging custom
        spacing otherwise.
    """
    if len(columns) < 2:
        return ColumnSpacingInfo(uniform=True, column_gap=None)
    ordered = sorted(columns, key=lambda zone: zone.bounds.x)
    gaps = [
        ordered[index + 1].bounds.x - ordered[index].bounds.right
        for index in range(len(ordered) - 1)
    ]
    average = sum(gaps) / len(gaps)
    uniform = all(abs(gap - average) <= tolerance for gap in gaps)
    return ColumnSpacingInfo(uniform=uniform, column_gap=average if uniform else None)


def distribute_columns_evenly(columns: Sequence[Zone]) -> tuple[Zone, ...]:
    """Redistribute intermediate columns evenly between a fixed first and last.

    Orders ``columns`` by their field's ``first_question`` (their logical
    reading order, independent of wherever they currently sit on the page),
    keeps the first and last zone's horizontal position exactly as it was, and
    moves every zone in between (via :func:`translate_zone`, which also shifts
    its bubble grid and any overrides) to land at evenly-interpolated x
    positions. Question numbers are never touched - only geometry moves.

    Args:
        columns: The sibling columns of one logical Question Region.

    Returns:
        The same zones (same ids, same order as the input was sorted into),
        with only the intermediate ones' geometry changed. Fewer than three
        columns has nothing to redistribute and is returned unchanged.
    """
    if len(columns) < 3:
        return tuple(columns)
    ordered = sorted(
        columns,
        key=lambda zone: (
            zone.field.first_question if isinstance(zone.field, QuestionBlockFieldDefinition) else 0
        ),
    )
    first_x = ordered[0].bounds.x
    last_x = ordered[-1].bounds.x
    step = (last_x - first_x) / (len(ordered) - 1)

    result: list[Zone] = [ordered[0]]
    for index, zone in enumerate(ordered[1:-1], start=1):
        target_x = first_x + index * step
        result.append(translate_zone(zone, dx=target_x - zone.bounds.x, dy=0.0))
    result.append(ordered[-1])
    return tuple(result)


def translate_zone(zone: Zone, *, dx: float, dy: float) -> Zone:
    """Return ``zone`` shifted by ``(dx, dy)`` in normalised coordinates.

    Used for a whole-region drag. The shift is clamped so the zone's bounds
    never leave the page (`0 <= x`, `x + width <= 1`, and likewise for `y`);
    every bubble centre and override, if any, is shifted by the *same, clamped*
    amount, so the grid stays exactly where it was relative to the region -
    only the region's position on the page changes, not its internal layout.

    Args:
        zone: The zone to move.
        dx: Requested horizontal shift, in normalised units.
        dy: Requested vertical shift, in normalised units.

    Returns:
        A new zone. When the requested shift is already within bounds, the
        result moves by exactly ``(dx, dy)``; otherwise it moves as far as it
        can in that direction before hitting the page edge.
    """
    new_x = _clamp_position(zone.bounds.x + dx, zone.bounds.width)
    new_y = _clamp_position(zone.bounds.y + dy, zone.bounds.height)
    actual_dx = new_x - zone.bounds.x
    actual_dy = new_y - zone.bounds.y

    new_bounds = NormalizedRect(
        x=new_x, y=new_y, width=zone.bounds.width, height=zone.bounds.height
    )
    if zone.grid is None:
        return zone.model_copy(update={"bounds": new_bounds})

    new_grid = BubbleGrid(
        origin=NormalizedPoint(
            x=zone.grid.origin.x + actual_dx, y=zone.grid.origin.y + actual_dy
        ),
        row_pitch=zone.grid.row_pitch,
        column_pitch=zone.grid.column_pitch,
        bubble_size=zone.grid.bubble_size,
        overrides=tuple(
            BubbleOverride(
                row=override.row,
                column=override.column,
                center=NormalizedPoint(
                    x=override.center.x + actual_dx, y=override.center.y + actual_dy
                ),
            )
            for override in zone.grid.overrides
        ),
    )
    return zone.model_copy(update={"bounds": new_bounds, "grid": new_grid})


def resize_zone(zone: Zone, *, bounds: NormalizedRect) -> Zone:
    """Return ``zone`` with its bounds changed and its grid re-fitted to them.

    Bubble size is preserved; the pitch is recomputed so the same number of
    rows and columns spread evenly across the new bounds (see
    :func:`fit_grid_to_bounds`). Any :class:`BubbleOverride` entries are
    dropped: they were positioned for the old layout, and silently stretching
    a hand-placed override along with a pitch it was specifically created to
    deviate from would move it somewhere the user never chose.

    Args:
        zone: The zone to resize.
        bounds: The new bounds. Must be large enough to hold the zone's
            existing bubble size (see :func:`fit_grid_to_bounds`).

    Returns:
        A new zone. Zones with no grid (``ignored`` regions) simply take the
        new bounds.
    """
    if zone.grid is None:
        return zone.model_copy(update={"bounds": bounds})

    # A zone with a grid is never `ignored` (Zone's own validator forbids the
    # combination), so `.rows`/`.columns` are always available here.
    assert not isinstance(zone.field, IgnoredFieldDefinition)
    new_grid = fit_grid_to_bounds(
        bounds=bounds,
        rows=zone.field.rows,
        columns=zone.field.columns,
        bubble_size=zone.grid.bubble_size,
    )
    return zone.model_copy(update={"bounds": bounds, "grid": new_grid})


def set_zone_bubble_size(zone: Zone, *, bubble_size: NormalizedSize) -> Zone:
    """Return ``zone`` with a different bubble size and **nothing else changed**.

    The grid's ``origin``, both pitches, every :class:`BubbleOverride` and the
    zone's ``bounds`` are carried across untouched, so:

    * every bubble centre stays exactly where it was;
    * the parent region does not move or resize.

    That is the property that makes bubble-size calibration predictable - the
    user grows or shrinks the measurement window around marks they have already
    aligned, rather than re-flowing the layout underneath themselves. Fitting
    the grid to a *new region size* is the opposite operation and lives in
    :func:`resize_zone`.

    Args:
        zone: The zone to restyle. A zone with no grid (an ignored region) is
            returned unchanged - there is nothing to size.
        bubble_size: The new bounding size of one bubble.

    Returns:
        A new zone.

    Raises:
        pydantic.ValidationError: The enlarged bubbles push the grid past the
            zone's bounds. (Only the *centres* are checked by ``Zone``; a bubble
            whose outline overhangs the region's edge is normal on a real sheet
            and is deliberately allowed.)
    """
    if zone.grid is None:
        return zone
    new_grid = BubbleGrid(
        origin=zone.grid.origin,
        row_pitch=zone.grid.row_pitch,
        column_pitch=zone.grid.column_pitch,
        bubble_size=bubble_size,
        overrides=zone.grid.overrides,
    )
    return zone.model_copy(update={"grid": new_grid})


def zone_inherits_bubble_size(zone: Zone, *, default: NormalizedSize) -> bool:
    """Whether ``zone``'s bubble size still matches the template default.

    OMRFlow expresses "this region uses the template's bubble radius" without a
    persisted flag: a region **inherits** for exactly as long as its stored size
    is what the default produces. Giving a region its own radius makes it differ,
    and :func:`apply_default_bubble_radius` then leaves it alone.

    Choosing equality over a stored flag means the relationship survives save and
    reload with no schema change, and cannot drift out of sync with the geometry
    it describes.

    Args:
        zone: Any zone; one with no grid never inherits (it has no bubbles).
        default: The size the template's current default radius produces.

    Returns:
        Whether the zone is currently inheriting.
    """
    if zone.grid is None:
        return False
    return _close_enough(zone.grid.bubble_size.width, default.width) and _close_enough(
        zone.grid.bubble_size.height, default.height
    )


def _close_enough(value: float, expected: float) -> bool:
    """Whether ``value`` is within :data:`BUBBLE_SIZE_RELATIVE_TOLERANCE` of ``expected``."""
    if expected <= 0.0:
        return value == expected
    return abs(value - expected) <= expected * BUBBLE_SIZE_RELATIVE_TOLERANCE


def apply_default_bubble_radius(
    template: OmrTemplate, *, radius: float
) -> OmrTemplate:
    """Set the template's default bubble radius and push it into inheriting zones.

    Every zone that :func:`zone_inherits_bubble_size` reports as inheriting the
    *previous* default is resized to the new one via :func:`set_zone_bubble_size`,
    so bubble centres and region rectangles are untouched. A zone with its own
    radius keeps it.

    Args:
        template: The document to update.
        radius: New default radius, normalised to the page width.

    Returns:
        A new template. One call, one document, so the designer's undo stack
        records one step for the whole change.

    Raises:
        ValueError: ``radius`` is not positive.
    """
    if radius <= 0.0:
        raise ValueError("Bubble radius must be positive")
    previous = template.default_bubble_size
    updated = template.model_copy(update={"default_bubble_radius": radius})
    new_size = updated.default_bubble_size
    zones = tuple(
        set_zone_bubble_size(zone, bubble_size=new_size)
        if zone_inherits_bubble_size(zone, default=previous)
        else zone
        for zone in updated.zones
    )
    return updated.model_copy(update={"zones": zones})


def _clamp_position(value: float, extent: float) -> float:
    """Clamp a rectangle's origin so ``[value, value + extent]`` stays in ``[0, 1]``."""
    return max(0.0, min(value, 1.0 - extent))


def generate_ignored_zone(
    *, zone_id: str, label: str, bounds: NormalizedRect, display_color: str | None = None
) -> Zone:
    """Build a region deliberately excluded from recognition (a logo, instructions)."""
    return Zone(
        id=zone_id,
        label=label,
        bounds=bounds,
        field=IgnoredFieldDefinition(type=FieldType.IGNORED),
        grid=None,
        display_color=display_color or DEFAULT_DISPLAY_COLORS[FieldType.IGNORED],
    )


@dataclass(frozen=True, slots=True)
class DesignerValidationReport:
    """Errors and warnings surfaced to a user before saving a template.

    Attributes:
        errors: Problems severe enough that the template should not be relied
            on - a duplicate question number, for instance. Saving is still
            permitted (a half-finished template is a normal thing to save and
            resume), but the designer must show these prominently.
        warnings: Unusual but possibly intentional situations - an empty
            template, a region overlapping a marker.
    """

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Whether there is nothing at all to show the user."""
        return not self.errors and not self.warnings


def validate_template_for_designer(template: OmrTemplate) -> DesignerValidationReport:
    """Check ``template`` for designer-relevant problems beyond schema validity.

    Everything :class:`~omr_scanner.domain.template.OmrTemplate` itself already
    enforces (four distinct marker roles, unique zone ids, grids that fit their
    bounds, threshold ordering) cannot appear here, because a template that
    violates any of it cannot exist as a Python object in the first place. This
    function catches the problems Pydantic construction *cannot* see: geometry
    that is merely inadvisable, and cross-zone relationships like duplicate or
    missing question numbers.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not template.zones:
        warnings.append("The template has no regions defined yet.")

    marker_boxes = {
        marker.role: NormalizedRect(
            x=marker.center.x - marker.size.width / 2.0,
            y=marker.center.y - marker.size.height / 2.0,
            width=marker.size.width,
            height=marker.size.height,
        )
        for marker in template.registration_markers
    }
    orientation_box = NormalizedRect(
        x=template.orientation_marker.center.x - template.orientation_marker.size.width / 2.0,
        y=template.orientation_marker.center.y - template.orientation_marker.size.height / 2.0,
        width=template.orientation_marker.size.width,
        height=template.orientation_marker.size.height,
    )

    for zone in template.zones:
        for role, box in marker_boxes.items():
            if zone.bounds.overlaps(box):
                warnings.append(
                    f"Region '{zone.label}' overlaps the {role.value.replace('_', ' ')} "
                    "registration marker."
                )
        if zone.bounds.overlaps(orientation_box):
            warnings.append(f"Region '{zone.label}' overlaps the orientation marker.")

    for i, first in enumerate(template.zones):
        for second in template.zones[i + 1 :]:
            if first.bounds.overlaps(second.bounds):
                warnings.append(f"Region '{first.label}' overlaps region '{second.label}'.")

    question_owners: dict[int, str] = {}
    for zone in template.zones:
        if not isinstance(zone.field, QuestionBlockFieldDefinition):
            continue
        for question in range(zone.field.first_question, zone.field.last_question + 1):
            if question in question_owners:
                errors.append(
                    f"Question {question} is defined in both '{question_owners[question]}' "
                    f"and '{zone.label}'."
                )
            else:
                question_owners[question] = zone.label

    if question_owners:
        covered = sorted(question_owners)
        expected = set(range(covered[0], covered[-1] + 1))
        missing = expected - set(covered)
        if missing:
            ranges = _format_ranges(sorted(missing))
            warnings.append(f"Question(s) {ranges} are not covered by any region.")

    return DesignerValidationReport(errors=tuple(errors), warnings=tuple(warnings))


def _format_ranges(numbers: Sequence[int]) -> str:
    """Format a sorted sequence of integers as compact ranges, e.g. ``"3-5, 9"``."""
    if not numbers:
        return ""
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = number
    ranges.append(f"{start}-{previous}" if start != previous else str(start))
    return ", ".join(ranges)


__all__ = [
    "BUBBLE_SIZE_RELATIVE_TOLERANCE",
    "DEFAULT_BUBBLE_RADIUS",
    "DEFAULT_BUBBLE_RADIUS_PX",
    "DEFAULT_DISPLAY_COLORS",
    "ColumnLayoutMode",
    "ColumnSpacingInfo",
    "DesignerValidationReport",
    "apply_default_bubble_radius",
    "build_blank_template",
    "default_bubble_radius_for",
    "distribute_columns_evenly",
    "fit_grid_to_bounds",
    "generate_character_grid_zone",
    "generate_column_array",
    "generate_ignored_zone",
    "generate_question_columns",
    "measure_column_gap",
    "place_grid_in_bounds",
    "resize_zone",
    "set_zone_bubble_size",
    "translate_zone",
    "validate_template_for_designer",
    "zone_inherits_bubble_size",
]
