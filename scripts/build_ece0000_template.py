"""Regenerate the calibrated ``.omrt`` template for ``examples/ECE-0000.png``.

Purpose:
    Ship one template that matches the repository's real sample sheet, so that
    Phase 3 recognition - and the GUI tests that drive it - can be validated
    against a genuine scan rather than only against synthetic pages.

Why a script rather than a hand-written JSON file:
    Every number below was *measured* from the sample: the registration squares
    and the orientation dash by Phase 1's own detector, and the three bubble
    lattices by fitting an evenly spaced grid to the printed rings found with a
    Hough transform. Keeping the measurement in a script records where the
    numbers came from and makes them reproducible; a hand-edited JSON file would
    not.

Where the coordinates may live:
    Here, and in tests as ground truth - never in ``src/``. The application must
    never carry coordinates specific to one image; see
    ``.claude/skills/qtguitesting/references/omrflow_gui_test_scenarios.md``.

Usage:
    python scripts/build_ece0000_template.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPOSITORY_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - convenience for direct execution
    sys.path.insert(0, str(_SRC))

from omr_scanner.domain.geometry import (  # noqa: E402
    NormalizedPoint,
    NormalizedRect,
    NormalizedSize,
)
from omr_scanner.domain.template import (  # noqa: E402
    BubbleGrid,
    FieldType,
    GridFieldDefinition,
    MarkerRole,
    MarkerShape,
    OmrTemplate,
    OrientationMarker,
    PageGeometry,
    QuestionBlockFieldDefinition,
    RegistrationMarker,
    SymbolAxis,
    Zone,
)
from omr_scanner.services.template_service import save_template  # noqa: E402

PAGE_WIDTH = 2480
PAGE_HEIGHT = 3508
"""The sample's own pixel size; the canonical page is deliberately identical, so
one canonical pixel is one scanned pixel and nothing is resampled twice."""

MARKER_CENTERS_PX: dict[MarkerRole, tuple[float, float]] = {
    MarkerRole.TOP_LEFT: (102.03, 250.49),
    MarkerRole.TOP_RIGHT: (2364.07, 257.70),
    MarkerRole.BOTTOM_RIGHT: (2353.95, 3203.58),
    MarkerRole.BOTTOM_LEFT: (91.95, 3189.54),
}
"""Registration-square centroids, as Phase 1's detector reports them."""

MARKER_SIZE_PX = (44.0, 44.0)
ORIENTATION_BOX_PX = (158.0, 309.0, 86.0, 44.0)
"""The orientation dash as ``(x, y, width, height)``; a 1.95:1 horizontal bar."""

BUBBLE_DIAMETER_PX = 36.0
"""Printed bubble diameter, from the median Hough radius of ~18 px."""

ROLL_LATTICE = (245.5, 46.94, 965.0, 47.578)
SET_LATTICE = (716.5, 46.50, 966.0, 47.628)
"""``(x0, column pitch, y0, row pitch)`` in canonical pixels."""

ANSWER_LATTICES = (
    (333.75, 47.07, 2205.00, 47.113),
    (757.00, 47.15, 2206.50, 47.163),
    (1180.50, 47.15, 2209.25, 47.213),
    (1603.50, 47.12, 2211.75, 47.213),
    (2028.50, 46.95, 2214.00, 47.263),
)
"""One lattice per printed answer column. The small drift in ``y0`` across the
page is real - the printing is not perfectly square to the registration marks -
and is exactly why each printed column is its own zone with its own grid."""

ROLL_DIGITS = 8
SET_DIGITS = 2
QUESTIONS_PER_BLOCK = 20
ANSWER_LABELS = ("a", "b", "c", "d")
DIGITS = tuple(str(digit) for digit in range(10))

ZONE_MARGIN_PX = 6.0
"""Slack between the outermost bubble edge and the zone rectangle, so a zone is
visibly a container rather than exactly its contents."""


def _normalised_grid(
    x0: float, dx: float, y0: float, dy: float
) -> tuple[BubbleGrid, tuple[float, float, float, float]]:
    """Build a bubble grid and report the pixel extent it needs, for the bounds."""
    grid = BubbleGrid(
        origin=NormalizedPoint(x=x0 / PAGE_WIDTH, y=y0 / PAGE_HEIGHT),
        column_pitch=dx / PAGE_WIDTH,
        row_pitch=dy / PAGE_HEIGHT,
        bubble_size=NormalizedSize(
            width=BUBBLE_DIAMETER_PX / PAGE_WIDTH,
            height=BUBBLE_DIAMETER_PX / PAGE_HEIGHT,
        ),
    )
    return grid, (x0, dx, y0, dy)


def _bounds(
    x0: float, dx: float, columns: int, y0: float, dy: float, rows: int
) -> NormalizedRect:
    """Rectangle enclosing a lattice plus one bubble radius and a small margin."""
    half = BUBBLE_DIAMETER_PX / 2.0 + ZONE_MARGIN_PX
    left = x0 - half
    top = y0 - half
    width = dx * (columns - 1) + 2 * half
    height = dy * (rows - 1) + 2 * half
    return NormalizedRect(
        x=max(left, 0.0) / PAGE_WIDTH,
        y=max(top, 0.0) / PAGE_HEIGHT,
        width=min(width, PAGE_WIDTH - left) / PAGE_WIDTH,
        height=min(height, PAGE_HEIGHT - top) / PAGE_HEIGHT,
    )


def build_template() -> OmrTemplate:
    """Assemble the calibrated template for the sample sheet."""
    markers = tuple(
        RegistrationMarker(
            role=role,
            shape=MarkerShape.FILLED_SQUARE,
            center=NormalizedPoint(x=x / PAGE_WIDTH, y=y / PAGE_HEIGHT),
            size=NormalizedSize(
                width=MARKER_SIZE_PX[0] / PAGE_WIDTH,
                height=MARKER_SIZE_PX[1] / PAGE_HEIGHT,
            ),
            search_radius=0.06,
        )
        for role, (x, y) in MARKER_CENTERS_PX.items()
    )

    ox, oy, ow, oh = ORIENTATION_BOX_PX
    orientation = OrientationMarker(
        shape=MarkerShape.FILLED_RECTANGLE,
        center=NormalizedPoint(x=(ox + ow / 2) / PAGE_WIDTH, y=(oy + oh / 2) / PAGE_HEIGHT),
        size=NormalizedSize(width=ow / PAGE_WIDTH, height=oh / PAGE_HEIGHT),
        expected_near=MarkerRole.TOP_LEFT,
        search_radius=0.06,
    )

    zones: list[Zone] = []

    roll_grid, (rx, rdx, ry, rdy) = _normalised_grid(*ROLL_LATTICE)
    zones.append(
        Zone(
            id="roll_number",
            label="Roll number",
            bounds=_bounds(rx, rdx, ROLL_DIGITS, ry, rdy, len(DIGITS)),
            field=GridFieldDefinition(
                type=FieldType.NUMERIC,
                symbols=DIGITS,
                character_count=ROLL_DIGITS,
                symbol_axis=SymbolAxis.VERTICAL,
            ),
            grid=roll_grid,
            display_color="#1E88E5",
        )
    )

    set_grid, (sx, sdx, sy, sdy) = _normalised_grid(*SET_LATTICE)
    zones.append(
        Zone(
            id="set_code",
            label="Set code",
            bounds=_bounds(sx, sdx, SET_DIGITS, sy, sdy, len(DIGITS)),
            field=GridFieldDefinition(
                type=FieldType.SET_CODE,
                symbols=DIGITS,
                character_count=SET_DIGITS,
                symbol_axis=SymbolAxis.VERTICAL,
            ),
            grid=set_grid,
            display_color="#8E24AA",
        )
    )

    for index, lattice in enumerate(ANSWER_LATTICES):
        grid, (qx, qdx, qy, qdy) = _normalised_grid(*lattice)
        first = index * QUESTIONS_PER_BLOCK + 1
        zones.append(
            Zone(
                id=f"questions_{index}",
                label=f"Questions {first}-{first + QUESTIONS_PER_BLOCK - 1}",
                bounds=_bounds(
                    qx, qdx, len(ANSWER_LABELS), qy, qdy, QUESTIONS_PER_BLOCK
                ),
                field=QuestionBlockFieldDefinition(
                    type=FieldType.QUESTION_BLOCK,
                    first_question=first,
                    question_count=QUESTIONS_PER_BLOCK,
                    answer_labels=ANSWER_LABELS,
                    symbol_axis=SymbolAxis.HORIZONTAL,
                    group_id="questions",
                ),
                grid=grid,
                display_color="#2E7D32",
            )
        )

    return OmrTemplate(
        name="ECE sample answer sheet",
        description=(
            "Calibrated against examples/ECE-0000.png: an 8-digit roll number, a "
            "two-digit set code and 100 questions in five printed columns of "
            "a/b/c/d. Regenerate with scripts/build_ece0000_template.py."
        ),
        page=PageGeometry(
            width_mm=210.0,
            height_mm=297.0,
            canonical_width_px=PAGE_WIDTH,
            canonical_height_px=PAGE_HEIGHT,
        ),
        registration_markers=markers,
        orientation_marker=orientation,
        zones=tuple(zones),
        reference_image="../ECE-0000.png",
        default_bubble_radius=(BUBBLE_DIAMETER_PX / 2.0) / PAGE_WIDTH,
    )


def main() -> int:
    """Write the template into ``examples/templates``."""
    destination = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
    template = build_template()
    written = save_template(template, destination)
    print(f"Wrote {written}")
    print(f"  zones: {len(template.zones)}")
    print(f"  bubbles: {sum(zone.bubble_count for zone in template.zones)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
