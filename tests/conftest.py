"""Shared pytest fixtures.

Purpose:
    Keep every test isolated from the machine it runs on. No test may read or
    write the real user configuration directory, the real log directory or any
    real project.

Key fixture:
    ``isolated_user_environment`` is autouse: it redirects the per-user
    configuration and log directories into a temporary folder for *every* test,
    including ones that never mention configuration.

Imaging fixtures:
    ``canonical_sheet`` renders the default synthetic page once per session -
    the geometry tests all start from the same ground truth, and rendering it per
    test would cost more than every alignment in the suite put together.

Recognition fixtures (Phase 3):
    ``answer_sheet_template`` builds a template whose geometry matches the
    synthetic page, and ``marked_sheet`` renders that page with chosen bubbles
    shaded. Together they give a recognition test *stated* ground truth - "Q3 is
    b, Q4 is blank, Q5 carries both a and c" - instead of an image somebody
    eyeballed once.

    The bubble positions come from the template itself via
    :func:`omr_scanner.recognition.fields.zone_groups`, the same function the
    recogniser uses, so a fixture and the code under test can never disagree
    about which cell is which - a disagreement would otherwise look like a
    recognition bug.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from omr_scanner.config.paths import ENV_CONFIG_DIR, ENV_LOG_DIR
from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    BubbleGrid,
    FieldType,
    GridFieldDefinition,
    MarkerRole,
    OmrTemplate,
    OrientationMarker,
    PageGeometry,
    QuestionBlockFieldDefinition,
    RegistrationMarker,
    SymbolAxis,
    Zone,
)
from omr_scanner.imaging import AlignmentConfig
from omr_scanner.imaging.config import DEFAULT_MARKER_TARGETS
from omr_scanner.imaging.synthetic import (
    AnswerBubbleSpec,
    SyntheticSheet,
    SyntheticSheetSpec,
    render_sheet,
)
from omr_scanner.recognition.fields import zone_groups
from omr_scanner.services import ProjectSession, create_project

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
"""Repository root, derived from this file's location rather than the CWD."""

EXAMPLE_TEMPLATE = REPOSITORY_ROOT / "resources" / "templates" / "example_answer_sheet.omrt"


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect per-user configuration and logs into the test's temp directory."""
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "user_config"))
    monkeypatch.setenv(ENV_LOG_DIR, str(tmp_path / "user_logs"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return an empty directory that may contain project folders."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    return workspace_dir


@pytest.fixture
def project_session(workspace: Path) -> Iterator[ProjectSession]:
    """Create a project and yield its open session, closing it afterwards.

    Closing matters on Windows: an open SQLite handle prevents pytest from
    removing the temporary directory.
    """
    session = create_project(workspace, "Sample Examination")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def logging_sandbox() -> Iterator[None]:
    """Restore the root logger after a test that configures logging.

    Logging is process-global; without this, a test that installs file handlers
    would leak them into every test that runs afterwards.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)


@pytest.fixture
def example_template_path() -> Path:
    """Path of the illustrative template shipped in ``resources/templates``."""
    return EXAMPLE_TEMPLATE


@pytest.fixture(scope="session")
def canonical_sheet() -> SyntheticSheet:
    """The default synthetic canonical page, with its ground-truth coordinates.

    Session scoped and never mutated: every alignment test warps a *copy* of it
    through a distortion, so sharing the rendered page is safe and saves the
    suite several hundred renders.
    """
    return render_sheet(SyntheticSheetSpec())


@pytest.fixture(scope="session")
def canonical_config() -> AlignmentConfig:
    """The alignment configuration matching :func:`canonical_sheet`.

    Frozen dataclasses all the way down, so sharing one instance across tests
    cannot leak state between them.
    """
    return AlignmentConfig()


# ----------------------------------------------------------------------
# Phase 3: templates and marked sheets for recognition tests
# ----------------------------------------------------------------------
SYNTHETIC_PAGE_WIDTH = 1240
SYNTHETIC_PAGE_HEIGHT = 1754

BUBBLE_WIDTH = 0.022
BUBBLE_HEIGHT = 0.0155
"""Bubble size on the synthetic answer sheet, normalised per axis.

Different numbers for the two axes because the page is taller than it is wide:
these describe a bubble that is *round in pixels*, which is what a printed
bubble is."""

_ROLL_ORIGIN = NormalizedPoint(x=0.10, y=0.14)
_ROLL_COLUMN_PITCH = 0.045
_ROLL_ROW_PITCH = 0.030
_SET_ORIGIN = NormalizedPoint(x=0.46, y=0.14)
_SET_COLUMN_PITCH = 0.045
_QUESTION_ORIGIN = NormalizedPoint(x=0.12, y=0.56)
_QUESTION_COLUMN_PITCH = 0.040
_QUESTION_ROW_PITCH = 0.018
_QUESTION_BLOCK_GAP = 0.34
"""Lattice geometry of the synthetic answer sheet, in normalised coordinates.

Chosen to sit clear of the registration markers, the orientation dash and the
decoy graphics `render_sheet` already draws, so that a recognition test measures
bubbles rather than clutter."""

DIGIT_SYMBOLS = tuple(str(digit) for digit in range(10))


def _grid_bounds(
    origin: NormalizedPoint, column_pitch: float, columns: int, row_pitch: float, rows: int
) -> NormalizedRect:
    """Rectangle enclosing a lattice plus a bubble's worth of margin."""
    half_x = BUBBLE_WIDTH / 2.0 + 0.004
    half_y = BUBBLE_HEIGHT / 2.0 + 0.004
    return NormalizedRect(
        x=origin.x - half_x,
        y=origin.y - half_y,
        width=column_pitch * (columns - 1) + 2 * half_x,
        height=row_pitch * (rows - 1) + 2 * half_y,
    )


def build_answer_sheet_template(
    *,
    name: str = "Synthetic answer sheet",
    roll_digits: int = 6,
    set_symbols: Sequence[str] = ("A", "B", "C", "D"),
    set_positions: int = 1,
    question_blocks: int = 2,
    questions_per_block: int = 10,
    answer_labels: Sequence[str] = ("A", "B", "C", "D"),
) -> OmrTemplate:
    """Build a template whose geometry matches the default synthetic page.

    Args:
        name: Template name.
        roll_digits: Character positions in the numeric identifier field.
        set_symbols: Symbols of the set-code field. Multi-character symbols
            (``"10"``, ``"A1"``) are as valid here as single letters.
        set_positions: Printed positions in the set-code field; more than one
            makes it a positional code.
        question_blocks: How many printed answer columns.
        questions_per_block: Questions in each.
        answer_labels: The options each question offers.

    Returns:
        A validated template using the same page size, marker positions and
        orientation mark as :func:`omr_scanner.imaging.synthetic.render_sheet`'s
        defaults, so a sheet rendered from it aligns with the template that
        describes it.
    """
    markers = tuple(
        RegistrationMarker(
            role=role,
            center=DEFAULT_MARKER_TARGETS[role],
            size=NormalizedSize(width=0.03, height=0.021),
        )
        for role in MarkerRole
    )
    orientation = OrientationMarker(
        center=NormalizedPoint(x=0.14, y=0.035),
        size=NormalizedSize(width=0.05, height=0.012),
        expected_near=MarkerRole.TOP_LEFT,
    )
    bubble = NormalizedSize(width=BUBBLE_WIDTH, height=BUBBLE_HEIGHT)
    zones: list[Zone] = [
        Zone(
            id="roll_number",
            label="Roll number",
            bounds=_grid_bounds(
                _ROLL_ORIGIN, _ROLL_COLUMN_PITCH, roll_digits, _ROLL_ROW_PITCH, 10
            ),
            field=GridFieldDefinition(
                type=FieldType.NUMERIC,
                symbols=DIGIT_SYMBOLS,
                character_count=roll_digits,
                symbol_axis=SymbolAxis.VERTICAL,
            ),
            grid=BubbleGrid(
                origin=_ROLL_ORIGIN,
                column_pitch=_ROLL_COLUMN_PITCH,
                row_pitch=_ROLL_ROW_PITCH,
                bubble_size=bubble,
            ),
        ),
        Zone(
            id="set_code",
            label="Set code",
            bounds=_grid_bounds(
                _SET_ORIGIN,
                _SET_COLUMN_PITCH,
                set_positions,
                _ROLL_ROW_PITCH,
                len(set_symbols),
            ),
            field=GridFieldDefinition(
                type=FieldType.SET_CODE,
                symbols=tuple(set_symbols),
                character_count=set_positions,
                symbol_axis=SymbolAxis.VERTICAL,
            ),
            grid=BubbleGrid(
                origin=_SET_ORIGIN,
                column_pitch=_SET_COLUMN_PITCH,
                row_pitch=_ROLL_ROW_PITCH,
                bubble_size=bubble,
            ),
        ),
    ]

    for index in range(question_blocks):
        origin = NormalizedPoint(
            x=_QUESTION_ORIGIN.x + index * _QUESTION_BLOCK_GAP, y=_QUESTION_ORIGIN.y
        )
        first = index * questions_per_block + 1
        zones.append(
            Zone(
                id=f"questions_{index}",
                label=f"Questions {first}-{first + questions_per_block - 1}",
                bounds=_grid_bounds(
                    origin,
                    _QUESTION_COLUMN_PITCH,
                    len(answer_labels),
                    _QUESTION_ROW_PITCH,
                    questions_per_block,
                ),
                field=QuestionBlockFieldDefinition(
                    type=FieldType.QUESTION_BLOCK,
                    first_question=first,
                    question_count=questions_per_block,
                    answer_labels=tuple(answer_labels),
                    symbol_axis=SymbolAxis.HORIZONTAL,
                    group_id="questions",
                ),
                grid=BubbleGrid(
                    origin=origin,
                    column_pitch=_QUESTION_COLUMN_PITCH,
                    row_pitch=_QUESTION_ROW_PITCH,
                    bubble_size=bubble,
                ),
            )
        )

    return OmrTemplate(
        name=name,
        page=PageGeometry(
            width_mm=210.0,
            height_mm=297.0,
            canonical_width_px=SYNTHETIC_PAGE_WIDTH,
            canonical_height_px=SYNTHETIC_PAGE_HEIGHT,
        ),
        registration_markers=markers,
        orientation_marker=orientation,
        zones=tuple(zones),
        default_bubble_radius=BUBBLE_WIDTH / 2.0,
    )


Selection = str | tuple[str, float] | Sequence[str] | None
"""What one response group contains on a rendered sheet.

``"B"`` marks B solidly; ``("B", 0.35)`` marks it faintly; ``["B", "D"]`` marks
both; ``None`` leaves the group blank."""


def _selection_fills(selection: Selection, labels: Sequence[str]) -> dict[str, float]:
    """Return the fill strength of each label under one selection."""
    if selection is None:
        return {}
    if isinstance(selection, str):
        return {selection: 1.0}
    if (
        isinstance(selection, tuple)
        and len(selection) == 2
        and isinstance(selection[0], str)
        and isinstance(selection[1], int | float)
    ):
        return {selection[0]: float(selection[1])}
    chosen = {}
    for item in selection:
        if isinstance(item, tuple):
            chosen[item[0]] = float(item[1])
        else:
            chosen[str(item)] = 1.0
    unknown = set(chosen) - set(labels)
    if unknown:
        raise ValueError(f"Selection names symbols the group does not have: {sorted(unknown)}")
    return chosen


def marked_sheet_spec(
    template: OmrTemplate,
    marks: Mapping[str, Mapping[int, Selection]] | None = None,
    *,
    base: SyntheticSheetSpec | None = None,
) -> SyntheticSheetSpec:
    """Build a synthetic-page specification that renders ``template``'s bubbles.

    Args:
        template: The template whose zones define where bubbles go.
        marks: What is marked, as ``{zone id: {group index: selection}}``. The
            group index is the character position for a grid field and the
            question *offset* within its block for a question block. Zones and
            groups left out are rendered empty.
        base: Starting page specification; the default draws the usual markers,
            dash and decoy graphics.

    Returns:
        A specification whose ``answer_bubbles`` cover every bubble of every
        non-ignored zone.
    """
    chosen = marks or {}
    start = base if base is not None else SyntheticSheetSpec()
    bubbles: list[AnswerBubbleSpec] = []

    for zone in template.zones:
        grid = zone.grid
        if grid is None:
            continue
        zone_marks = chosen.get(zone.id, {})
        for group in zone_groups(zone):
            fills = _selection_fills(zone_marks.get(group.key), group.labels)
            for index, cell in enumerate(group.cells):
                center = grid.bubble_center(*cell)
                label = group.labels[index]
                bubbles.append(
                    AnswerBubbleSpec(
                        center=center,
                        width=grid.bubble_size.width,
                        height=grid.bubble_size.height,
                        fill=fills.get(label, 0.0),
                        symbol=label if len(label) == 1 else "",
                    )
                )

    return SyntheticSheetSpec(
        width=template.page.canonical_width_px,
        height=template.page.canonical_height_px,
        marker_targets=start.marker_targets,
        marker_width=start.marker_width,
        marker_height=start.marker_height,
        orientation_center=start.orientation_center,
        orientation_width=start.orientation_width,
        orientation_height=start.orientation_height,
        control_points=(),
        draw_bubbles=False,
        # The default page's decoy text bars and answer frames are placed for
        # the *marker detection* tests and sit across the answer area, which
        # would corrupt the very measurements a recognition test is checking.
        # The symbol printed inside every empty bubble supplies the realistic
        # "there is ink here already" challenge instead.
        draw_text_bars=False,
        draw_answer_frames=False,
        decoy_markers=start.decoy_markers,
        omit_markers=start.omit_markers,
        omit_orientation_marker=start.omit_orientation_marker,
        answer_bubbles=tuple(bubbles),
    )


def render_marked_sheet(
    template: OmrTemplate,
    marks: Mapping[str, Mapping[int, Selection]] | None = None,
    *,
    base: SyntheticSheetSpec | None = None,
) -> NDArray[np.uint8]:
    """Render ``template``'s page with ``marks`` shaded in."""
    return render_sheet(marked_sheet_spec(template, marks, base=base)).image


@pytest.fixture
def answer_sheet_template() -> OmrTemplate:
    """A template matching the synthetic page: roll, set code and 20 questions."""
    return build_answer_sheet_template()


@pytest.fixture
def marked_sheet() -> Callable[..., NDArray[np.uint8]]:
    """Factory rendering a template's page with chosen bubbles marked."""
    return render_marked_sheet


@pytest.fixture
def write_marked_sheet(tmp_path: Path) -> Callable[..., Path]:
    """Factory rendering a marked sheet straight to a PNG file.

    Recognition runs from a file, so most Phase 3 tests want a path rather than
    an array; this keeps the ``cv2.imwrite`` boilerplate out of every one.
    """
    import cv2

    def write(
        template: OmrTemplate,
        marks: Mapping[str, Mapping[int, Selection]] | None = None,
        *,
        name: str = "sheet.png",
        base: SyntheticSheetSpec | None = None,
        image: NDArray[np.uint8] | None = None,
    ) -> Path:
        rendered = image if image is not None else render_marked_sheet(template, marks, base=base)
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(destination), rendered)
        return destination

    return write
