"""The write-in boxes above a grid field: where they are, and keeping them clean.

Purpose:
    A real answer sheet asks the candidate to *write* their identifier in a
    row of boxes and then bubble it underneath. A synthetic script that fills
    the bubbles and leaves the boxes empty - or, on a reference scan, leaves
    somebody else's handwriting in them - does not look like a completed
    script, and it cannot stage the case reconciliation most needs: a number
    written correctly and bubbled wrongly, which an operator resolves by
    reading the boxes.

Responsibilities:
    * :func:`write_in_rows` - derive each box's position from the bubble grid
      the template already declares.
    * :func:`prepare_reference_write_in` - on a real blank scan, locate the
      printed boxes precisely and erase any handwriting already in them, once
      per run.

Where the geometry comes from, and why no template field was added:
    ``.omrt`` templates describe bubbles, not boxes, and the schema forbids
    unknown keys - so a new field would make every template written by this
    version unreadable by the previous one. The boxes are instead *derived*:

    ```text
        [ c0 ][ c1 ][ c2 ] ...   <- one box per character column, centred on
         ^ gap                      that column's bubbles, one column pitch
        (0)   (0)   (0)             wide and WriteInProportions.height_rows
        (1)   (1)   (1)             row pitches tall, sitting gap_rows row
        ...                         pitches above the first bubble's top edge
    ```

    Every quantity is a multiple of the grid's own pitch, so the row scales
    with the template and the render DPI and assumes no character count. The
    default proportions were measured on this repository's reference form
    (``Sample-Project/1.Template/ECE-0000.png``: boxes 1.50 row pitches tall,
    bottom border 0.30 row pitch above the first bubble, box centres within
    3 px of the bubble columns at 300 dpi). On a reference scan the derived
    boxes are only a starting point - :func:`prepare_reference_write_in` snaps
    them to the printed lines it actually finds.

    Only a field whose characters run in *columns* (symbols running down each
    column, :attr:`~omr_scanner.domain.template.SymbolAxis.VERTICAL`) gets a
    row: that is the layout that prints boxes above the bubbles. Any other
    layout gets none, rather than boxes in an invented place.

What does NOT belong here:
    * Drawing characters - :mod:`omr_scanner.imaging.synthetic` does that, where
      it is told to.
    * Deciding *what* is written - that is the case plan's
      ``intended_roll``, never the bubbles.
"""

from __future__ import annotations

import itertools
import logging
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.domain.template import (
    FieldType,
    GridFieldDefinition,
    IgnoredFieldDefinition,
    SymbolAxis,
)
from omr_scanner.recognition.fields import zone_groups

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate, Zone

_LOGGER = logging.getLogger(__name__)

IDENTIFIER_ROLE = "identifier"
SET_CODE_ROLE = "set_code"


@dataclass(frozen=True, slots=True)
class WriteInCell:
    """One write-in box, in normalised canonical page coordinates.

    Plain floats rather than a validated ``NormalizedRect``: these are built
    once per run and read for every sheet, and a derived box may legitimately
    touch the page edge that the validated type would reject.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        """Horizontal centre."""
        return self.x + self.width / 2.0

    @property
    def center_y(self) -> float:
        """Vertical centre."""
        return self.y + self.height / 2.0


@dataclass(frozen=True, slots=True)
class WriteInRow:
    """The boxes above one grid field, one per character position.

    Attributes:
        zone_id: The template zone the boxes belong to.
        role: :data:`IDENTIFIER_ROLE` or :data:`SET_CODE_ROLE`.
        cells: One box per character position, left to right.
        refined: ``True`` once the boxes were snapped to printed lines on a
            real scan; ``False`` while they are only derived from the grid.
            When refined, each cell is the box's *interior* - inside its
            printed border.
        symbols: The symbols the field's bubbles offer, so what is written can
            be held to what the field can actually spell.
    """

    zone_id: str
    role: str
    cells: tuple[WriteInCell, ...]
    refined: bool = False
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WriteInProportions:
    """How a write-in row relates to its bubble grid, in grid pitches.

    Attributes:
        height_rows: Box height, in row pitches.
        gap_rows: Space between the box's bottom border and the top edge of the
            first bubble row, in row pitches.
    """

    height_rows: float = 1.50
    gap_rows: float = 0.30


DEFAULT_PROPORTIONS = WriteInProportions()


def write_in_rows(
    template: OmrTemplate,
    proportions: WriteInProportions = DEFAULT_PROPORTIONS,
) -> tuple[WriteInRow, ...]:
    """Return the write-in row above each identifier and set-code field.

    Args:
        template: The template whose grids the boxes are derived from.
        proportions: The row's size and spacing relative to its grid.

    Returns:
        Identifier rows first, then set-code rows. A field that cannot carry a
        row - laid out in rows rather than columns, or with no room above it on
        the page - contributes nothing and is logged.
    """
    rows: list[WriteInRow] = []
    for zone in template.zones:
        field = zone.field
        if zone.grid is None or isinstance(field, IgnoredFieldDefinition):
            continue
        if not isinstance(field, GridFieldDefinition):
            continue
        # The same test `FieldLayout.of` uses to find the identifier, so the
        # boxes written in and the bubbles marked are always the same field.
        if field.type.value in {"numeric", "candidate_id"}:
            role = IDENTIFIER_ROLE
        elif field.type is FieldType.SET_CODE:
            role = SET_CODE_ROLE
        else:
            continue
        row = _row_for_zone(zone, role, proportions)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda item: item.role != IDENTIFIER_ROLE)
    return tuple(rows)


def identifier_row(rows: Sequence[WriteInRow]) -> WriteInRow | None:
    """The first identifier row, which is the one a candidate's number goes in.

    The first, matching :meth:`~omr_scanner.evaluation.test_cases.FieldLayout.of`,
    which also takes the first identifier zone as *the* identifier.
    """
    return next((row for row in rows if row.role == IDENTIFIER_ROLE), None)


def set_code_row(rows: Sequence[WriteInRow]) -> WriteInRow | None:
    """The first set-code row - the field :class:`FieldLayout` reads the set from."""
    return next((row for row in rows if row.role == SET_CODE_ROLE), None)


def _row_for_zone(
    zone: Zone, role: str, proportions: WriteInProportions
) -> WriteInRow | None:
    """Derive the boxes above one column-laid grid field."""
    grid = zone.grid
    field = zone.field
    if grid is None or not isinstance(field, GridFieldDefinition):
        return None
    if field.symbol_axis is not SymbolAxis.VERTICAL:
        _LOGGER.info(
            "Zone '%s' lays its characters out in rows; no write-in boxes derived",
            zone.id,
        )
        return None

    groups = zone_groups(zone)
    if not groups:
        return None

    centres_x: list[float] = []
    top = 1.0
    for group in groups:
        centres = [grid.bubble_center(*cell) for cell in group.cells]
        centres_x.append(statistics.fmean(point.x for point in centres))
        top = min(top, min(point.y for point in centres) - grid.bubble_size.height / 2.0)

    spacing = [b - a for a, b in itertools.pairwise(centres_x)]
    # One box per column pitch, measured between the columns actually present
    # so that an override-shifted column does not shrink every box. A single
    # column has no neighbour to measure against; its declared pitch, or a
    # bubble and a margin when the pitch is meaningless, stands in.
    width = (
        statistics.median(spacing)
        if spacing
        else max(grid.column_pitch, grid.bubble_size.width * 1.4)
    )
    height = proportions.height_rows * grid.row_pitch
    bottom = top - proportions.gap_rows * grid.row_pitch
    y = bottom - height
    if y < 0.0 or height <= 0.0 or width <= 0.0:
        _LOGGER.info(
            "Zone '%s' has no room above its bubbles for write-in boxes", zone.id
        )
        return None

    cells = tuple(
        WriteInCell(x=centre - width / 2.0, y=y, width=width, height=height)
        for centre in centres_x
    )
    return WriteInRow(zone_id=zone.id, role=role, cells=cells, symbols=tuple(field.symbols))


# ----------------------------------------------------------------------
# Real reference scans: snapping to printed lines, erasing old handwriting
# ----------------------------------------------------------------------
_ROI_MARGIN = 0.45
"""How far beyond the derived row the working window reaches, in box sizes."""

_SEARCH_FRACTION_Y = 0.35
"""How far a horizontal border may be from its derived position, in box heights."""

_SEARCH_FRACTION_X = 0.30
"""How far a vertical border may be from its derived position, in box widths.
Under half a box, so the search for one border can never reach the next."""

_LINE_COVERAGE_H = 0.75
"""Share of a row a horizontal line must darken to count as printed border.

Measured across 10-90 % of the row's width. A printed border covers nearly
all of it; the top arc of a blank bubble ring just below the row covers under
half, which is the false match this is set high enough to refuse."""

_LINE_COVERAGE_V = 0.70
"""The same for a vertical line, over one box's height. Higher, because a
handwritten ``1`` is a vertical stroke too - but never one that spans a box."""

_LINE_EXTENT_RATIO = 0.85
"""A located line's thickness: the adjacent rows/columns whose coverage is at
least this share of the peak's. A printed line - halo included - is dark
along nearly all its length, so each of its columns scores near the peak; a
handwritten stroke touching it scores well below, and must not widen it into
the box (measured on the reference form: a stroke beside a border at ~0.6)."""

_PITCH_TOLERANCE = 0.10
"""How far a located vertical border may sit off the even pitch the others
share, in box widths, before it is taken for a stroke and set aside."""

_DARK_RATIO = 0.80
"""A pixel darker than this fraction of the paper level is printed or written."""

_INK_RATIO = 0.72
"""Inside a box, a pixel darker than this fraction of the local paper level is
handwriting to erase. Chosen to leave a form's pale cell tint alone (measured
at 83 % of paper on the reference form) while catching ballpoint and pencil."""

_CROSSING_RATIO = 0.60
"""On a border, a pixel darker than this fraction of the border's own median
level is handwriting crossing it. Measured on the reference form: its pink
border sits at grey ~139, ballpoint at ~50."""

_CROSSING_MARGIN = 40.0
"""...and darker than the border's level by at least this many grey levels.

A ratio alone fails for a near-black border: 60 % of a core at grey 7 is
4, well inside the scatter that sensor noise and JPEG ringing give the core's
own pixels, so the line itself would be read as a crossing stroke. No pen
is 40 levels darker than a black line, which is the point: such a border is
left as scanned."""

_BORDER_BAND = 0.18
"""How far beyond the interiors the border rule applies, in box sizes: enough
to cover a printed border and the margin inside it, short of the heading
above and the first bubble row below."""

_FALLBACK_INSET = 0.14
"""Inset from a border that could not be located, in box sizes - generous, so
an unfound line is never erased as though it were handwriting."""


@dataclass(frozen=True, slots=True)
class WriteInPreparation:
    """What :func:`prepare_reference_write_in` found and did, for the manifest.

    Attributes:
        rows: The rows, snapped to the printed boxes where found. A row whose
            boxes are not printed on this form keeps its derived geometry
            (``refined`` stays ``False``).
        borders_found: Box borders located on the scan, over the rows whose
            boxes are printed.
        borders_expected: Box borders looked for in those rows.
        erased_pixels: Handwriting pixels removed from the boxes, in scan
            pixels. Zero on a genuinely blank reference.
        unprinted_rows: Zone ids of the rows with no printed boxes on this
            scan - a form that prints boxes above its identifier but not above
            its set code, say. Nothing in them is snapped or erased.
    """

    rows: tuple[WriteInRow, ...]
    borders_found: int
    borders_expected: int
    erased_pixels: int
    unprinted_rows: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        """As JSON-safe plain data."""
        return {
            "rows": [
                {
                    "zone_id": row.zone_id,
                    "role": row.role,
                    "cells": len(row.cells),
                    "refined": row.refined,
                }
                for row in self.rows
            ],
            "borders_found": self.borders_found,
            "borders_expected": self.borders_expected,
            "erased_pixels": self.erased_pixels,
            "unprinted_rows": list(self.unprinted_rows),
        }


def prepare_reference_write_in(
    image: NDArray[np.uint8],
    canonical_to_scan: NDArray[np.float64],
    rows: Sequence[WriteInRow],
    *,
    canonical_width: int,
    canonical_height: int,
    erase: bool = True,
) -> tuple[NDArray[np.uint8], WriteInPreparation]:
    """Locate the printed write-in boxes on a real scan and empty them.

    Done **once per run**, on the reference itself, so every generated sheet
    starts from boxes that contain nothing - a reference photographed from a
    used script would otherwise carry its old number under every new one.

    Each row is worked on in the canonical frame (a small window warped out of
    the scan), so a reference fed rotated or askew is handled exactly as a
    straight one. Only ink is replaced: inside a located border, what is
    darker than the local paper; on a border, what is darker than the border.
    Where a stroke covered part of a printed line, that stretch of line is
    rebuilt from the line's own appearance elsewhere along it, and the rest is
    inpainted and given back the paper's texture. Cell tints, headings and
    everything else on the form are left as scanned.

    Args:
        image: The reference scan, grey or BGR. Read only.
        canonical_to_scan: Homography from canonical page pixels to ``image``.
        rows: Derived rows, from :func:`write_in_rows`.
        canonical_width: Canonical page width the rows are normalised to.
        canonical_height: Canonical page height.
        erase: Remove handwriting; ``False`` only snaps the geometry.

    Returns:
        The cleaned image (a new array; ``image`` is untouched) and a record of
        what was found.
    """
    result = image.copy()
    refined_rows: list[WriteInRow] = []
    unprinted: list[str] = []
    found = expected = erased = 0
    scan_to_canonical = np.linalg.inv(canonical_to_scan)

    for row in rows:
        window = _row_window(row, canonical_width, canonical_height)
        x0, y0, x1, y1 = window
        to_window = _translation(-x0, -y0) @ scan_to_canonical
        roi = np.asarray(
            cv2.warpPerspective(
                result,
                to_window,
                (x1 - x0, y1 - y0),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            ),
            dtype=np.uint8,
        )
        grey = _grey(roi)
        snap = _snap_row(grey, row, x0, y0, canonical_width, canonical_height)
        interiors = snap.interiors
        if not snap.printed:
            # No box of this row's own is on the form. Whatever sits where one
            # would be - a heading, a neighbouring row's edge - is not
            # handwriting, so it is neither counted nor erased.
            unprinted.append(row.zone_id)
            refined_rows.append(row)
            continue
        found += snap.found
        expected += snap.expected
        refined_rows.append(
            WriteInRow(
                zone_id=row.zone_id,
                role=row.role,
                cells=tuple(
                    WriteInCell(
                        x=(x0 + left) / canonical_width,
                        y=(y0 + top) / canonical_height,
                        width=(right - left) / canonical_width,
                        height=(bottom - top) / canonical_height,
                    )
                    for left, top, right, bottom in interiors
                ),
                refined=True,
                symbols=row.symbols,
            )
        )
        if erase:
            erased += _erase_handwriting(result, roi, grey, snap, window, canonical_to_scan)

    preparation = WriteInPreparation(
        rows=tuple(refined_rows),
        borders_found=found,
        borders_expected=expected,
        erased_pixels=erased,
        unprinted_rows=tuple(unprinted),
    )
    _LOGGER.info(
        "Reference write-in boxes: %d/%d border(s) located, %d handwriting "
        "pixel(s) erased, row(s) not printed: %s",
        found,
        expected,
        erased,
        ", ".join(unprinted) or "none",
    )
    return result, preparation


def _row_window(
    row: WriteInRow, canonical_width: int, canonical_height: int
) -> tuple[int, int, int, int]:
    """The canonical-pixel window a row is examined in, clipped to the page."""
    left = min(cell.x for cell in row.cells)
    right = max(cell.x + cell.width for cell in row.cells)
    top = min(cell.y for cell in row.cells)
    bottom = max(cell.y + cell.height for cell in row.cells)
    pad_x = _ROI_MARGIN * row.cells[0].width
    pad_y = _ROI_MARGIN * row.cells[0].height
    return (
        max(0, int((left - pad_x) * canonical_width)),
        max(0, int((top - pad_y) * canonical_height)),
        min(canonical_width, int(np.ceil((right + pad_x) * canonical_width))),
        min(canonical_height, int(np.ceil((bottom + pad_y) * canonical_height))),
    )


def _translation(dx: float, dy: float) -> NDArray[np.float64]:
    """A 3x3 translation."""
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]], dtype=np.float64)


def _grey(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """One channel, whatever arrived."""
    if image.ndim == 2:
        return image
    return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)


def _paper_level(grey: NDArray[np.uint8]) -> float:
    """The brightness of bare paper in a region.

    A high percentile, not the max, so one blown-out pixel does not set it.
    """
    return max(float(np.percentile(grey, 95)), 1.0)


@dataclass(frozen=True, slots=True)
class _RowSnap:
    """What :func:`_snap_row` found for one row, in window pixels.

    Attributes:
        interiors: One ``(left, top, right, bottom)`` interior per cell: the
            box inside its border and a small margin, where text is drawn.
        inners: The same boxes out to the located lines themselves, where ink
            is erased.
        found: Borders located.
        expected: Borders looked for.
        printed: Whether the row's boxes are printed on this scan at all.
        line_level: The grey level of the located lines' cores; ``None`` when
            no line was located.
        lines: Each located line as ``(axis, first, last, start, stop)``:
            ``axis`` 0 for horizontal, its thickness ``first..last`` across
            it, and ``start:stop`` along it.
    """

    interiors: list[tuple[int, int, int, int]]
    inners: list[tuple[int, int, int, int]]
    found: int
    expected: int
    printed: bool
    line_level: float | None
    lines: list[tuple[int, int, int, int, int]]


def _snap_row(
    grey: NDArray[np.uint8],
    row: WriteInRow,
    x0: int,
    y0: int,
    canonical_width: int,
    canonical_height: int,
) -> _RowSnap:
    """Find each box's interior in a canonical window.

    A row counts as printed when at least one of its horizontal borders and
    at least half of its vertical ones are found. A single located line is
    not enough: a derived box can share an edge with the neighbouring row's
    last box and so "find" that box's border, and a stray rule can sit where
    a top border would be - either would otherwise license erasing whatever
    is printed in a box that is not there.
    """
    dark = grey < _DARK_RATIO * _paper_level(grey)
    cells = [
        (
            cell.x * canonical_width - x0,
            cell.y * canonical_height - y0,
            (cell.x + cell.width) * canonical_width - x0,
            (cell.y + cell.height) * canonical_height - y0,
        )
        for cell in row.cells
    ]
    box_w = cells[0][2] - cells[0][0]
    box_h = cells[0][3] - cells[0][1]
    span = (int(cells[0][0] + 0.1 * box_w), int(cells[-1][2] - 0.1 * box_w))

    top_line = _find_line(dark, cells[0][1], box_h * _SEARCH_FRACTION_Y, span, axis=0,
                          coverage=_LINE_COVERAGE_H)
    bottom_line = _find_line(dark, cells[0][3], box_h * _SEARCH_FRACTION_Y, span, axis=0,
                             coverage=_LINE_COVERAGE_H)
    inset_y = _FALLBACK_INSET * box_h
    top = top_line[1] + 1 if top_line else round(cells[0][1] + inset_y)
    bottom = bottom_line[0] - 1 if bottom_line else round(cells[0][3] - inset_y)
    horizontal_found = int(top_line is not None) + int(bottom_line is not None)

    # Shared borders: the edge between two boxes is one printed line.
    edges = [cells[0][0]]
    edges += [(a[2] + b[0]) / 2.0 for a, b in itertools.pairwise(cells)]
    edges.append(cells[-1][2])
    vertical_span = (top, bottom)
    lines = [
        _find_line(dark, edge, box_w * _SEARCH_FRACTION_X, vertical_span, axis=1,
                   coverage=_LINE_COVERAGE_V)
        for edge in edges
    ]
    lines = _on_even_pitch(lines, box_w * _PITCH_TOLERANCE)
    vertical_found = sum(line is not None for line in lines)
    printed = horizontal_found >= 1 and 2 * vertical_found >= len(edges)

    margin = max(1, round(0.03 * box_w))
    inset_x = _FALLBACK_INSET * box_w
    interiors: list[tuple[int, int, int, int]] = []
    inners: list[tuple[int, int, int, int]] = []
    for index, cell in enumerate(cells):
        left_line, right_line = lines[index], lines[index + 1]
        left = left_line[1] + 1 + margin if left_line else round(cell[0] + inset_x)
        right = right_line[0] - 1 - margin if right_line else round(cell[2] - inset_x)
        interior = (left, top + margin, right, bottom - margin)
        if interior[2] - interior[0] < 2 or interior[3] - interior[1] < 2:
            # Nothing sensible was found; fall back to the derived box, inset.
            interior = (
                round(cell[0] + inset_x), round(cell[1] + inset_y),
                round(cell[2] - inset_x), round(cell[3] - inset_y),
            )
        interiors.append(interior)
        # Where ink is looked for: right up to each located line, so a stroke
        # written against a border goes too; the inset interior where a line
        # was not located, so an unfound border is never taken for ink.
        inners.append((
            left_line[1] + 1 if left_line else interior[0],
            top_line[1] + 1 if top_line else interior[1],
            right_line[0] if right_line else interior[2],
            bottom_line[0] if bottom_line else interior[3],
        ))

    # How dark the printed lines are: at each point along a located line, the
    # darkest pixel across its thickness - its core, not the grey halo that
    # blur and compression spread around it - then the median along every
    # line, which a gap or a stroke crossing it cannot move.
    cores: list[NDArray[np.uint8]] = []
    for line in (top_line, bottom_line):
        if line is not None:
            cores.append(grey[line[0] : line[1] + 1, max(0, span[0]) : span[1]].min(axis=0))
    for line in lines:
        if line is not None and bottom > top:
            cores.append(grey[top:bottom, line[0] : line[1] + 1].min(axis=1))
    line_level = float(np.median(np.concatenate(cores))) if cores else None

    # Each line's full extent, corner to corner, for rebuilding it where a
    # stroke covered it.
    height, width = grey.shape
    x_start = lines[0][0] if lines[0] else max(0, round(cells[0][0]))
    x_stop = lines[-1][1] + 1 if lines[-1] else min(width, round(cells[-1][2]))
    y_start = top_line[0] if top_line else max(0, top)
    y_stop = bottom_line[1] + 1 if bottom_line else min(height, bottom)
    extents = [(0, *line, x_start, x_stop) for line in (top_line, bottom_line) if line]
    extents += [(1, *line, y_start, y_stop) for line in lines if line]

    return _RowSnap(
        interiors=interiors,
        inners=inners,
        found=horizontal_found + vertical_found,
        expected=2 + len(edges),
        printed=printed,
        line_level=line_level,
        lines=extents,
    )


def _on_even_pitch(
    lines: list[tuple[int, int] | None], tolerance: float
) -> list[tuple[int, int] | None]:
    """Set aside a vertical border that breaks the row's even pitch.

    The borders of one printed row are evenly spaced, so a line fitted through
    the located ones predicts each. One found far off that fit is a stroke
    that happened to run parallel to where a border was expected; it is
    dropped - the box then falls back to its derived edge - and the fit
    repeated, worst first, while at least three borders remain to fit.
    """
    kept = list(lines)
    while True:
        found = [(i, (line[0] + line[1]) / 2.0) for i, line in enumerate(kept) if line]
        if len(found) < 3:
            return kept
        index = np.array([i for i, _ in found], dtype=np.float64)
        centre = np.array([c for _, c in found], dtype=np.float64)
        slope, offset = np.polyfit(index, centre, 1)
        residual = np.abs(centre - (slope * index + offset))
        worst = int(np.argmax(residual))
        if residual[worst] <= tolerance:
            return kept
        kept[found[worst][0]] = None


def _find_line(
    dark: NDArray[np.bool_],
    expected: float,
    reach: float,
    span: tuple[int, int],
    *,
    axis: int,
    coverage: float,
) -> tuple[int, int] | None:
    """Locate a printed line near ``expected``, as its ``(first, last)`` index.

    ``axis=0`` looks for a horizontal line (a row index), measuring coverage
    across ``span`` columns; ``axis=1`` for a vertical one. Returns ``None``
    when nothing in reach is dark along enough of the span to be printed.
    """
    limit = dark.shape[0] if axis == 0 else dark.shape[1]
    low = max(0, int(expected - reach))
    high = min(limit, int(np.ceil(expected + reach)) + 1)
    start, stop = max(0, span[0]), min(dark.shape[1] if axis == 0 else dark.shape[0], span[1])
    if high <= low or stop <= start:
        return None
    band = dark[low:high, start:stop] if axis == 0 else dark[start:stop, low:high].T
    scores = band.mean(axis=1)
    peak = int(np.argmax(scores))
    if scores[peak] < coverage:
        return None
    # The line's full thickness: every adjacent index still clearly part of it.
    threshold = _LINE_EXTENT_RATIO * float(scores[peak])
    first = last = peak
    while first > 0 and scores[first - 1] >= threshold:
        first -= 1
    while last < len(scores) - 1 and scores[last + 1] >= threshold:
        last += 1
    return low + first, low + last


def _erase_handwriting(
    image: NDArray[np.uint8],
    roi: NDArray[np.uint8],
    grey: NDArray[np.uint8],
    snap: _RowSnap,
    window: tuple[int, int, int, int],
    canonical_to_scan: NDArray[np.float64],
) -> int:
    """Remove the ink found in and around each box, in place, in scan pixels.

    Two rules decide what is ink, because handwriting does not respect a
    border:

    * **inside** a box, anything clearly darker than the paper is ink;
    * **on or beside** a border, only what is clearly darker than the *border
      itself* is - measured at the located lines' cores (``line_level``). A
      stroke that crosses a coloured or grey border is caught; a black-printed
      border is as dark as any pen, so nothing on it qualifies and it is left
      as scanned. Measuring at the core matters: the median of every dark
      pixel near a blurred black line is its grey halo, against which the
      line's own core would read as handwriting and be erased.

    Then three steps replace it:

    1. each stretch of located line that the ink covered is **rebuilt** by
       copying the line's own cross-section from the nearest uncovered point
       along it - its real colour, width and edge, never a drawn line;
    2. the remaining ink is inpainted from the paper around it - the lines
       themselves held out as sources, so a fill beside a border takes none
       of the border's colour;
    3. the inpainted pixels get the paper's **texture** back, as residuals
       drawn from the boxes' clean pixels, so a scan's grain and a form's
       dithered tint do not turn into a smooth patch. The draw is seeded, so
       preparing the same reference twice gives the same pixels.
    """
    interiors = snap.inners
    line_level = snap.line_level
    inside = np.zeros(grey.shape, dtype=np.uint8)
    mask = np.zeros(grey.shape, dtype=np.uint8)
    for left, top, right, bottom in interiors:
        region = grey[top:bottom, left:right]
        if region.size == 0:
            continue
        inside[top:bottom, left:right] = 255
        ink = region < _INK_RATIO * _paper_level(region)
        mask[top:bottom, left:right][ink] = 255

    # The band holding the borders: the interiors' extent, grown by enough to
    # cover a border and the margin inside it, and no further.
    band_left = min(box[0] for box in interiors)
    band_right = max(box[2] for box in interiors)
    band_top = min(box[1] for box in interiors)
    band_bottom = max(box[3] for box in interiors)
    reach_x = round(_BORDER_BAND * (band_right - band_left) / max(len(interiors), 1))
    reach_y = round(_BORDER_BAND * (band_bottom - band_top))
    bx0, by0 = max(0, band_left - reach_x), max(0, band_top - reach_y)
    bx1 = min(grey.shape[1], band_right + reach_x)
    by1 = min(grey.shape[0], band_bottom + reach_y)
    band = grey[by0:by1, bx0:bx1]
    borders = inside[by0:by1, bx0:bx1] == 0
    crossing_mask = np.zeros_like(mask)
    if line_level is not None:
        limit_level = min(_CROSSING_RATIO * line_level, line_level - _CROSSING_MARGIN)
        crossing = borders & (band < limit_level)
        crossing_mask[by0:by1, bx0:bx1][crossing] = 255
    if not (mask.any() or crossing_mask.any()):
        return 0
    # Grown so a stroke's anti-aliased edge goes with it - generously inside a
    # box, where only paper surrounds it, and by one pixel on a border, where
    # every pixel grown is border that inpainting has to rebuild. Then clipped
    # to the band, so no growth reaches the heading or the bubbles.
    kernel = np.ones((3, 3), dtype=np.uint8)
    grown = cv2.bitwise_or(
        cv2.dilate(mask, kernel, iterations=2), cv2.dilate(crossing_mask, kernel, iterations=1)
    )
    limit = np.zeros_like(mask)
    limit[by0:by1, bx0:bx1] = 255
    mask = np.asarray(cv2.bitwise_and(grown, limit), dtype=np.uint8)

    rebuilt_roi = roi.copy()
    rebuilt = _rebuild_lines(rebuilt_roi, mask, snap.lines)
    mask[rebuilt > 0] = 0

    # Back into the scan: only the patch the window covers, never a full page.
    x0, y0, x1, y1 = window
    corners = np.array([[[x0, y0]], [[x1, y0]], [[x1, y1]], [[x0, y1]]], dtype=np.float64)
    placed = cv2.perspectiveTransform(corners, canonical_to_scan).reshape(-1, 2)
    height, width = image.shape[:2]
    sx0 = max(0, int(np.floor(placed[:, 0].min())) - 2)
    sy0 = max(0, int(np.floor(placed[:, 1].min())) - 2)
    sx1 = min(width, int(np.ceil(placed[:, 0].max())) + 2)
    sy1 = min(height, int(np.ceil(placed[:, 1].max())) + 2)
    if sx1 <= sx0 or sy1 <= sy0:
        return 0
    to_patch = _translation(-sx0, -sy0) @ canonical_to_scan @ _translation(x0, y0)
    size = (sx1 - sx0, sy1 - sy0)

    def to_scan(layer: NDArray[np.uint8], flags: int) -> NDArray[np.uint8]:
        return np.asarray(
            cv2.warpPerspective(layer, to_patch, size, flags=flags,
                                borderMode=cv2.BORDER_REPLICATE),
            dtype=np.uint8,
        )

    patch_mask = to_scan(mask, cv2.INTER_NEAREST)
    patch_rebuilt = to_scan(rebuilt, cv2.INTER_NEAREST) > 0
    if not (patch_mask.any() or patch_rebuilt.any()):
        return 0
    patch = image[sy0:sy1, sx0:sx1].copy()
    if patch_rebuilt.any():
        patch[patch_rebuilt] = to_scan(rebuilt_roi, cv2.INTER_LINEAR)[patch_rebuilt]
    if patch_mask.any():
        filling = patch_mask > 0
        clean_inside = (to_scan(inside, cv2.INTER_NEAREST) > 0) & ~filling
        # Inpainting fills from every pixel around the ink, and beside a border
        # that includes the line itself - so a stroke against a black line is
        # "restored" dark, and one against a pink line leaves a pink blot.
        # The lines stand in as paper while the fill is computed, then return.
        on_line = (to_scan(_line_bands(mask.shape, snap.lines), cv2.INTER_NEAREST) > 0) & ~filling
        source = patch.copy()
        if on_line.any() and clean_inside.any():
            source[on_line] = np.median(patch[clean_inside], axis=0).astype(np.uint8)
        filled = np.asarray(cv2.inpaint(source, patch_mask, 3, cv2.INPAINT_TELEA), dtype=np.uint8)
        filled[on_line] = patch[on_line]
        patch = _restore_texture(filled, filling, clean_inside)
    image[sy0:sy1, sx0:sx1] = patch
    return int(np.count_nonzero(patch_mask) + np.count_nonzero(patch_rebuilt))


def _rebuild_lines(
    roi: NDArray[np.uint8],
    mask: NDArray[np.uint8],
    lines: Sequence[tuple[int, int, int, int, int]],
) -> NDArray[np.uint8]:
    """Rebuild, in ``roi`` in place, each stretch of line that ``mask`` covers.

    A line is walked along its length; wherever any pixel across its
    thickness is ink, the whole cross-section is copied from the nearest
    position along the same line with none. A printed gap stays a gap - its
    nearest clean position is the gap itself - so nothing is invented. Where
    another line crosses it is never a source: a corner's cross-section is
    the other line's full width, and copied along a border it would print a
    heavy bar.

    Returns:
        The rebuilt pixels, as a 0/255 mask.
    """
    rebuilt = np.zeros(mask.shape, dtype=np.uint8)
    for axis, first, last, start, stop in lines:
        junction = np.zeros(stop - start, dtype=bool)
        for other_axis, other_first, other_last, _start, _stop in lines:
            if other_axis != axis:
                junction[max(0, other_first - start) : max(0, other_last + 1 - start)] = True
        across = slice(first, last + 1)
        along = slice(start, stop)
        if axis == 0:
            # Views with the line's length first, so one indexing serves both.
            pixels = np.swapaxes(roi[across, along], 0, 1)
            ink = np.swapaxes(mask[across, along], 0, 1)
            marks = np.swapaxes(rebuilt[across, along], 0, 1)
        else:
            pixels, ink, marks = roi[along, across], mask[along, across], rebuilt[along, across]
        covered = ink.any(axis=1)
        clean = np.flatnonzero(~covered & ~junction[: len(covered)])
        targets = np.flatnonzero(covered)
        if targets.size == 0 or clean.size == 0:
            continue
        after = np.clip(np.searchsorted(clean, targets), 0, clean.size - 1)
        before = np.clip(after - 1, 0, clean.size - 1)
        nearer = np.where(
            np.abs(clean[before] - targets) <= np.abs(clean[after] - targets),
            clean[before],
            clean[after],
        )
        pixels[targets] = pixels[nearer]
        marks[targets] = 255
    return rebuilt


def _line_bands(
    shape: tuple[int, ...], lines: Sequence[tuple[int, int, int, int, int]]
) -> NDArray[np.uint8]:
    """The located lines' pixels, as a 0/255 mask."""
    bands = np.zeros(shape[:2], dtype=np.uint8)
    for axis, first, last, start, stop in lines:
        if axis == 0:
            bands[first : last + 1, start:stop] = 255
        else:
            bands[start:stop, first : last + 1] = 255
    return bands


def _restore_texture(
    patch: NDArray[np.uint8],
    filled: NDArray[np.bool_],
    inside: NDArray[np.bool_],
) -> NDArray[np.uint8]:
    """Give inpainted pixels the grain of the clean paper inside the boxes."""
    clean = inside & ~filled
    if not clean.any() or not filled.any():
        return patch
    samples = patch[clean].astype(np.int16)
    residuals = samples - np.median(samples, axis=0).astype(np.int16)
    generator = np.random.default_rng(0)
    picked = residuals[generator.integers(0, len(residuals), int(np.count_nonzero(filled)))]
    result = patch.copy()
    result[filled] = np.clip(patch[filled].astype(np.int16) + picked, 0, 255).astype(np.uint8)
    return result


__all__ = [
    "DEFAULT_PROPORTIONS",
    "IDENTIFIER_ROLE",
    "SET_CODE_ROLE",
    "WriteInCell",
    "WriteInPreparation",
    "WriteInProportions",
    "WriteInRow",
    "identifier_row",
    "prepare_reference_write_in",
    "set_code_row",
    "write_in_rows",
]
