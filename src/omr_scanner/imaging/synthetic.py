"""Synthetic OMR sheets and controlled distortions of them.

Purpose:
    Make alignment accuracy a *measured number* rather than an opinion. A sheet
    rendered from a known specification and then distorted by a known homography
    has ground truth attached: every claim about how well the engine recovered
    the page can be checked against it.

Responsibilities:
    * Render a canonical page carrying four registration markers, an orientation
      mark, interior control points and realistic competing graphics.
    * Apply reproducible translation, scale, rotation, perspective, brightness,
      blur, noise and JPEG degradation, returning the exact homography applied.

Why the interior control points matter:
    The homography is fitted through the four marker centres, so those four
    points map onto their targets to numerical precision no matter how wrong the
    detection was. Reprojection error over them therefore measures arithmetic,
    not geometry. The control points are *not* used in the fit, so the distance
    between where one of them ends up and where it should be is an honest
    measure of how much of the page was actually recovered.

Why the competing graphics matter:
    A sheet that is white paper plus four black squares makes any detector look
    good. These pages also carry hollow answer boxes the same size as a marker,
    rings of unfilled bubbles, text-like bars and optional solid decoy squares
    placed inside the corner search regions, because those are the shapes that
    defeat a detector relying on outline alone.

Marked bubbles (Phase 3):
    :attr:`SyntheticSheetSpec.answer_bubbles` renders bubbles at *given*
    positions, optionally shaded and optionally with a symbol printed inside the
    ring - the arrangement a real sheet uses, and the one that makes an empty
    bubble contain ink. That is what lets a recognition test state its ground
    truth ("Q7 is b, Q8 is blank, Q9 is b and d") and check it, with the same
    controlled distortions the geometry tests already use.

    The positions are plain normalised coordinates, not a template: this module
    must not know that ``.omrt`` documents exist (``docs/ARCHITECTURE.md``).
    Deriving the positions from a template is the caller's job - the test
    fixtures in ``tests/conftest.py`` do it.

What does NOT belong here:
    * Any knowledge of templates, fields or answers. This module draws circles
      where it is told to.
    * File I/O.

Status:
    A development and test utility. It is shipped inside the package rather than
    under ``tests/`` so that the developer tools in ``omr_scanner.tools`` can
    produce a sheet to work on without an external fixture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.domain.geometry import NormalizedPoint
from omr_scanner.domain.template import MarkerRole
from omr_scanner.imaging.config import (
    DEFAULT_CANONICAL_HEIGHT_PX,
    DEFAULT_CANONICAL_WIDTH_PX,
    DEFAULT_MARKER_TARGETS,
)
from omr_scanner.imaging.metrics import BubbleMetricsConfig
from omr_scanner.imaging.models import CANONICAL_CORNER_ORDER, Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from numpy.typing import NDArray

DEFAULT_CONTROL_POINTS: tuple[NormalizedPoint, ...] = tuple(
    NormalizedPoint(x=x, y=y)
    for y in (0.25, 0.45, 0.80)
    for x in (0.25, 0.50, 0.75)
)
"""Interior reference positions, spread across the page.

Two constraints shape these positions. They are kept well away from the four
marker corners, because a control point beside a marker would measure what the
fit already guarantees rather than the recovered geometry, and the middle of the
page is where an inaccurate homography goes wrong first. They are also kept clear
of the bubble grid and the text bars, so that a test which re-detects a control
point in the rectified page finds only that point.
"""

DEFAULT_ORIENTATION_CENTER = NormalizedPoint(x=0.14, y=0.035)
"""Where the orientation dash sits on the default synthetic sheet.

Defined as a module constant rather than inline so that it is constructed once:
the dataclass default must be a value, not a call evaluated per instance.
"""

_PAPER = 255
_INK = 0
_CONTROL_POINT_RADIUS_RATIO = 0.004
"""Control-point radius as a fraction of page width; small enough that the
marker size filter rejects it outright."""

_LINE_THICKNESS_PX = 2
"""Stroke width for hollow decoy shapes, in canonical pixels."""


class MarkStyle(StrEnum):
    """How a candidate's mark was made.

    Real candidates do not all shade neatly inside the ring, and a recognition
    engine that has only ever seen a concentric disc has been tested on the easy
    case. These are the shapes that actually turn up on collected sheets,
    modelled just far enough to exercise the *measurement* - none of them is an
    attempt at photorealistic handwriting, which would be a graphics project
    rather than a recognition one.
    """

    FILL = "fill"
    """A shaded disc covering ``fill`` of the measurable interior. The default,
    and what the instructions on a sheet ask for."""

    RING = "ring"
    """The bubble circled rather than filled - a thick outline with a hollow
    middle, which defeats a naive centre-pixel test."""

    TICK = "tick"
    """A check mark through the bubble."""

    CROSS = "cross"
    """Two diagonal strokes."""

    SCRIBBLE = "scribble"
    """A hurried back-and-forth scrawl, covering the bubble unevenly."""

    DOT = "dot"
    """A small spot: a stray pen touch, or an answer barely begun."""

    STROKE_H = "stroke_h"
    """A single horizontal line through the bubble."""

    STROKE_V = "stroke_v"
    """A single vertical line through the bubble."""

    SLASH = "slash"
    """One diagonal stroke - half a cross, and a genuinely common way of
    answering a paper form."""


@dataclass(frozen=True, slots=True)
class AnswerBubbleSpec:
    """One printed answer bubble, optionally marked.

    Attributes:
        center: Normalised centre on the canonical page.
        width: Bubble width as a fraction of page width.
        height: Bubble height as a fraction of page height.
        fill: How much of the bubble's *measurable interior* is inked, in
            ``[0, 1]`` - the same quantity
            :attr:`~omr_scanner.domain.template.RecognitionSettings.fill_ratio_threshold`
            is expressed in, so a test can say ``("B", 0.35)`` and mean "a mark
            that should land between the blank and fill thresholds". ``0.0``
            leaves the bubble empty and ``1.0`` covers it completely.

            Modelled as *partial coverage* rather than lighter grey because
            that is what a half-hearted pencil mark physically is: a scribble
            over part of the bubble, not a uniform wash. A uniform grey would
            make every measurement either 0 or 1 depending on which side of the
            ink threshold it fell, which would test nothing.
        symbol: A character printed inside the ring, as real sheets do (ⓐ ⓑ ⓒ,
            or the digit of a roll-number column). Empty for a plain ring.
            Present because it is the *reason* an empty bubble contains ink, and
            a measurement test that omits it is testing an easier problem than
            the real one.
        style: How the mark was made; see :class:`MarkStyle`. Every style other
            than :attr:`MarkStyle.FILL` interprets ``fill`` as "how much of the
            bubble the stroke covers", which is approximate by nature - the
            point of these is that the *measurement* meets an uneven, partial,
            off-centre mark, not that the shape is reproducible to the pixel.
        intensity: Darkness of the mark, ``0`` (invisible) to ``1`` (solid
            black). A hard pencil on a bright scanner lands near ``0.4``; a
            ballpoint at ``1.0``. Separate from ``fill`` because *how much* of a
            bubble is covered and *how dark* the covering is are two different
            failures, and a real engine has to survive both.
        offset_x: Mark centre displacement, as a fraction of the bubble's half
            width. ``0.5`` puts the mark half a radius to the right - a
            candidate marking between two bubbles.
        offset_y: The same, vertically.
        size_scale: Multiplies the mark's size. Below ``1`` an undersized mark
            that a generous sample might miss; above ``1`` one that spills over
            the printed ring into its neighbours.
    """

    center: NormalizedPoint
    width: float
    height: float
    fill: float = 0.0
    symbol: str = ""
    style: MarkStyle = MarkStyle.FILL
    intensity: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    size_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class SyntheticSheetSpec:
    """What a synthetic canonical page contains.

    Attributes:
        width: Canonical page width in pixels.
        height: Canonical page height in pixels.
        marker_targets: Normalised centre of each registration marker.
        marker_width: Marker width as a fraction of page width.
        marker_height: Marker height as a fraction of page height.
        orientation_center: Normalised centre of the orientation dash.
        orientation_width: Dash width as a fraction of page width.
        orientation_height: Dash height as a fraction of page height.
        control_points: Normalised positions of the interior reference dots.
        draw_bubbles: Render a grid of unfilled bubbles (rings whose interior is
            paper, so a fill test must reject them).
        draw_text_bars: Render long thin filled bars standing in for printed
            text (extreme aspect ratio, so an aspect test must reject them).
        draw_answer_frames: Render hollow rectangles the same size as a marker,
            including inside the corner search regions (identical outline to a
            marker, so only the interior-ink test rejects them).
        decoy_markers: Normalised centres of *solid* squares of marker size,
            rendered in addition to the real markers. They exist to prove that
            selection uses position, not just "the four largest black shapes".
        omit_markers: Corner roles whose marker is not rendered, for the
            missing-marker failure tests.
        omit_orientation_marker: Render no orientation dash.
        answer_bubbles: Printed answer bubbles at explicit positions, optionally
            shaded - see :class:`AnswerBubbleSpec`. Empty by default, so every
            existing geometry test renders exactly the page it did before.
    """

    width: int = DEFAULT_CANONICAL_WIDTH_PX
    height: int = DEFAULT_CANONICAL_HEIGHT_PX
    marker_targets: Mapping[MarkerRole, NormalizedPoint] = DEFAULT_MARKER_TARGETS
    marker_width: float = 0.03
    marker_height: float = 0.021
    orientation_center: NormalizedPoint = DEFAULT_ORIENTATION_CENTER
    orientation_width: float = 0.05
    orientation_height: float = 0.012
    control_points: tuple[NormalizedPoint, ...] = DEFAULT_CONTROL_POINTS
    draw_bubbles: bool = True
    draw_text_bars: bool = True
    draw_answer_frames: bool = True
    decoy_markers: tuple[NormalizedPoint, ...] = ()
    omit_markers: frozenset[MarkerRole] = frozenset()
    faint_markers: frozenset[MarkerRole] = frozenset()
    damaged_markers: frozenset[MarkerRole] = frozenset()
    faint_orientation_marker: bool = False
    omit_orientation_marker: bool = False
    answer_bubbles: tuple[AnswerBubbleSpec, ...] = ()

    def __post_init__(self) -> None:
        """Reject a specification that could not be rendered."""
        if self.width < 1 or self.height < 1:
            raise ValueError("Synthetic page dimensions must be positive")
        missing = set(CANONICAL_CORNER_ORDER) - set(self.marker_targets)
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"marker_targets is missing corner role(s): {names}")

    def to_pixels(self, point: NormalizedPoint) -> Point:
        """Project a normalised position onto this page, in pixels."""
        return Point(x=point.x * self.width, y=point.y * self.height)


@dataclass(frozen=True, slots=True)
class SyntheticSheet:
    """A rendered canonical page and the ground truth that goes with it.

    Attributes:
        image: Grayscale canonical page.
        marker_centers: The four marker centres in
            :data:`~omr_scanner.imaging.models.CANONICAL_CORNER_ORDER`, in
            canonical pixels. Exact by construction.
        control_points: Interior reference positions in canonical pixels.
        spec: The specification the page was rendered from.
    """

    image: NDArray[np.uint8]
    marker_centers: tuple[Point, ...]
    control_points: tuple[Point, ...]
    spec: SyntheticSheetSpec


@dataclass(frozen=True, slots=True)
class DistortionSpec:
    """A reproducible degradation of a canonical page.

    Every field is a physical description rather than an OpenCV argument, so a
    failing case reads as "10 degrees plus 6 per cent perspective" rather than
    as a matrix.

    Attributes:
        rotation_degrees: Clockwise rotation about the page centre.
        scale_x: Horizontal scale factor.
        scale_y: Vertical scale factor. Differs from ``scale_x`` only for the
            non-uniform-scale cases.
        translate_x_px: Extra horizontal offset in output pixels.
        translate_y_px: Extra vertical offset in output pixels.
        perspective_strength: Each page corner is displaced by up to this
            fraction of the shorter page side, in a direction drawn from the
            seeded generator. ``0.0`` leaves the page planar-parallel.
        margin_px: Blank border kept around the transformed page, standing in
            for the platen area around a sheet. Set to a negative value to crop
            into the page instead.
        brightness_gain: Multiplicative exposure factor; below 1 darkens.
        brightness_offset: Additive exposure offset in grey levels.
        illumination_gradient: Strength of a diagonal illumination ramp across
            the scan, as a fraction of full range. ``0.4`` darkens one corner to
            60 per cent of the other's brightness. This is the degradation a
            global threshold is actually vulnerable to - a page lifted off the
            platen, or the shadow of a bound spine - whereas a uniform exposure
            change leaves a histogram-derived threshold unaffected.
        blur_kernel_px: Gaussian blur kernel side; must be odd, ``0`` disables.
        noise_sigma: Standard deviation of additive Gaussian noise, in grey
            levels.
        jpeg_quality: Re-encode through JPEG at this quality (1-100) to
            introduce ringing around the markers; ``None`` skips it.
        seed: Seed for every random choice, so a failure is reproducible.
    """

    rotation_degrees: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    translate_x_px: float = 0.0
    translate_y_px: float = 0.0
    perspective_strength: float = 0.0
    margin_px: int = 40
    brightness_gain: float = 1.0
    brightness_offset: float = 0.0
    illumination_gradient: float = 0.0
    blur_kernel_px: int = 0
    noise_sigma: float = 0.0
    jpeg_quality: int | None = None
    paper_gray: int = _PAPER
    speckle_density: float = 0.0
    streak_strength: float = 0.0
    edge_shadow: float = 0.0
    seed: int = 0

    def __post_init__(self) -> None:
        """Reject a distortion that could not be applied."""
        if self.scale_x <= 0.0 or self.scale_y <= 0.0:
            raise ValueError("Scale factors must be positive")
        if self.blur_kernel_px < 0 or (self.blur_kernel_px and self.blur_kernel_px % 2 == 0):
            raise ValueError("blur_kernel_px must be zero or odd")
        if self.noise_sigma < 0.0:
            raise ValueError("noise_sigma must not be negative")
        if self.jpeg_quality is not None and not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must lie in [1, 100]")
        if not 0.0 <= self.illumination_gradient < 1.0:
            raise ValueError("illumination_gradient must lie in [0, 1)")


@dataclass(frozen=True, slots=True)
class DistortedSheet:
    """A degraded scan together with the transform that produced it.

    Attributes:
        image: The synthetic scan.
        homography: 3x3 matrix mapping canonical page pixels to scan pixels.
            The alignment engine's job is to recover its inverse.
        marker_centers: True marker centres in scan pixels.
        control_points: True control-point positions in scan pixels.
        source: The canonical sheet this was made from.
        spec: The distortion that was applied.
    """

    image: NDArray[np.uint8]
    homography: NDArray[np.float64]
    marker_centers: tuple[Point, ...]
    control_points: tuple[Point, ...]
    source: SyntheticSheet
    spec: DistortionSpec = field(default_factory=DistortionSpec)


def render_sheet(spec: SyntheticSheetSpec | None = None) -> SyntheticSheet:
    """Render a canonical synthetic OMR page.

    Args:
        spec: What the page contains. Defaults to a full page: four markers, an
            orientation dash, nine control points and the competing graphics.

    Returns:
        The page and its exact ground-truth coordinates.
    """
    active = spec if spec is not None else SyntheticSheetSpec()
    image: NDArray[np.uint8] = np.full((active.height, active.width), _PAPER, dtype=np.uint8)

    if active.draw_text_bars:
        _draw_text_bars(image, active)
    if active.draw_bubbles:
        _draw_bubbles(image, active)
    if active.draw_answer_frames:
        _draw_answer_frames(image, active)
    if active.answer_bubbles:
        _draw_answer_bubbles(image, active)

    marker_size = (active.marker_width * active.width, active.marker_height * active.height)
    for role in CANONICAL_CORNER_ORDER:
        if role in active.omit_markers:
            continue
        centre = active.to_pixels(active.marker_targets[role])
        # A faint marker is printed grey rather than black - a worn printer or
        # a pale photocopy - and a damaged one has a corner torn out of it.
        # Both are detector problems rather than page problems, which is
        # exactly why they are drawn here and not applied as a distortion.
        faint = role in active.faint_markers
        _draw_filled_rectangle(
            image, centre, marker_size, grey=_FAINT_MARKER_GREY if faint else _INK
        )
        if role in active.damaged_markers:
            _damage_rectangle(image, centre, marker_size)

    for decoy in active.decoy_markers:
        _draw_filled_rectangle(image, active.to_pixels(decoy), marker_size)

    if not active.omit_orientation_marker:
        _draw_filled_rectangle(
            image,
            active.to_pixels(active.orientation_center),
            (active.orientation_width * active.width, active.orientation_height * active.height),
            grey=_FAINT_MARKER_GREY if active.faint_orientation_marker else _INK,
        )

    control_points = tuple(active.to_pixels(point) for point in active.control_points)
    radius = max(2, round(_CONTROL_POINT_RADIUS_RATIO * active.width))
    for point in control_points:
        cv2.circle(image, (round(point.x), round(point.y)), radius, _INK, thickness=cv2.FILLED)

    marker_centers = tuple(
        active.to_pixels(active.marker_targets[role]) for role in CANONICAL_CORNER_ORDER
    )
    return SyntheticSheet(
        image=image,
        marker_centers=marker_centers,
        control_points=control_points,
        spec=active,
    )


def distortion_homography(
    sheet: SyntheticSheet, spec: DistortionSpec
) -> tuple[NDArray[np.float64], tuple[int, int]]:
    """Return the homography a distortion applies, and the canvas it needs.

    The page corners are scaled and rotated about the centre, displaced for
    perspective, then translated so that the whole transformed page - plus the
    configured margin - fits the output canvas. Deriving the canvas from the
    transformed corners is what makes a rotated page grow its own bounding box,
    exactly as a scanner's output does when a sheet is fed askew.

    Returns:
        ``(homography, (width, height))``.
    """
    width, height = float(sheet.spec.width), float(sheet.spec.height)
    corners = np.array(
        [[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]], dtype=np.float64
    )

    center = np.array([width / 2.0, height / 2.0], dtype=np.float64)
    scaled = (corners - center) * np.array([spec.scale_x, spec.scale_y], dtype=np.float64)

    angle = np.deg2rad(spec.rotation_degrees)
    rotation = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], dtype=np.float64
    )
    rotated = scaled @ rotation.T

    if spec.perspective_strength > 0.0:
        generator = np.random.default_rng(spec.seed)
        reach = spec.perspective_strength * min(width, height)
        rotated = rotated + generator.uniform(-reach, reach, size=rotated.shape)

    minimum = rotated.min(axis=0)
    offset = np.array(
        [spec.margin_px - minimum[0] + spec.translate_x_px,
         spec.margin_px - minimum[1] + spec.translate_y_px],
        dtype=np.float64,
    )
    placed = rotated + offset

    extent = rotated.max(axis=0) - minimum
    canvas = (
        max(1, round(float(extent[0]) + 2 * spec.margin_px)),
        max(1, round(float(extent[1]) + 2 * spec.margin_px)),
    )
    homography = np.asarray(
        cv2.getPerspectiveTransform(
            corners.astype(np.float32), placed.astype(np.float32)
        ),
        dtype=np.float64,
    )
    return homography, canvas


def apply_distortion(sheet: SyntheticSheet, spec: DistortionSpec) -> DistortedSheet:
    """Produce a synthetic scan of ``sheet`` under ``spec``.

    Geometry is applied first and photometry second, matching the physical
    order: the page is placed on the platen, then exposed, then compressed.
    """
    homography, canvas = distortion_homography(sheet, spec)
    image = np.asarray(
        cv2.warpPerspective(
            sheet.image,
            homography,
            canvas,
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(_PAPER, _PAPER, _PAPER, _PAPER),
        ),
        dtype=np.uint8,
    )

    if spec.brightness_gain != 1.0 or spec.brightness_offset != 0.0:
        image = np.asarray(
            cv2.convertScaleAbs(image, alpha=spec.brightness_gain, beta=spec.brightness_offset),
            dtype=np.uint8,
        )
    if spec.illumination_gradient > 0.0:
        image = _apply_illumination_gradient(image, spec.illumination_gradient)
    if spec.blur_kernel_px:
        image = np.asarray(
            cv2.GaussianBlur(image, (spec.blur_kernel_px, spec.blur_kernel_px), 0),
            dtype=np.uint8,
        )
    if spec.noise_sigma > 0.0:
        generator = np.random.default_rng(spec.seed + 1)
        noise = generator.normal(0.0, spec.noise_sigma, size=image.shape)
        image = np.clip(image.astype(np.float64) + noise, 0, 255).astype(np.uint8)
    if spec.paper_gray < _PAPER:
        # Grey or tinted stock: everything paper-coloured darkens, ink does not.
        image = _tint_paper(image, spec.paper_gray)
    if spec.speckle_density > 0.0:
        image = _add_speckle(image, spec.speckle_density, spec.seed + 2)
    if spec.streak_strength > 0.0:
        image = _add_scanner_streak(image, spec.streak_strength, spec.seed + 3)
    if spec.edge_shadow > 0.0:
        image = _add_edge_shadow(image, spec.edge_shadow)
    if spec.jpeg_quality is not None:
        encoded, buffer = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), spec.jpeg_quality]
        )
        if not encoded:
            raise RuntimeError("JPEG encoding of the synthetic scan failed")
        image = np.asarray(cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE), dtype=np.uint8)

    return DistortedSheet(
        image=image,
        homography=homography,
        marker_centers=project(homography, sheet.marker_centers),
        control_points=project(homography, sheet.control_points),
        source=sheet,
        spec=spec,
    )


def control_point_errors(
    transform: NDArray[np.float64], distorted: DistortedSheet
) -> tuple[float, ...]:
    """Measure how accurately a recovered transform rectifies the page.

    Each control point's true position in the distorted scan is mapped through
    ``transform`` and compared with the canonical position it was rendered at.
    The control points take no part in fitting the homography, so this is a
    measurement of the recovered geometry rather than of the fit's own residual.

    Args:
        transform: The scan-to-canonical homography the engine produced.
        distorted: The synthetic scan, carrying its ground truth.

    Returns:
        One distance in canonical pixels per control point. Only meaningful when
        the transform maps onto a canonical page the same size as the one the
        sheet was rendered at; otherwise the comparison is between two different
        coordinate systems.
    """
    recovered = project(transform, distorted.control_points)
    return tuple(
        actual.distance_to(expected)
        for actual, expected in zip(
            recovered, distorted.source.control_points, strict=True
        )
    )


def project(matrix: NDArray[np.float64], points: Sequence[Point]) -> tuple[Point, ...]:
    """Map ``points`` through a homography, as plain arithmetic.

    Kept separate from :func:`omr_scanner.imaging.geometry.apply_transform` on
    purpose: the test ground truth must not be computed by the code under test.
    """
    if not points:
        return ()
    homogeneous = np.array([[point.x, point.y, 1.0] for point in points], dtype=np.float64)
    mapped = homogeneous @ matrix.T
    return tuple(
        Point(x=float(row[0] / row[2]), y=float(row[1] / row[2])) for row in mapped
    )


def _tint_paper(image: NDArray[np.uint8], paper_gray: int) -> NDArray[np.uint8]:
    """Darken the page towards ``paper_gray`` without touching the ink.

    Scaled rather than offset, so black stays black: grey paper makes the
    *contrast* between mark and background smaller, which is the property that
    matters to a threshold, and an offset would simply lift everything.
    """
    factor = max(min(paper_gray, _PAPER), 1) / float(_PAPER)
    tinted: NDArray[np.uint8] = np.clip(
        image.astype(np.float32) * factor, 0, 255
    ).astype(np.uint8)
    return tinted


def _add_speckle(
    image: NDArray[np.uint8], density: float, seed: int
) -> NDArray[np.uint8]:
    """Scatter salt-and-pepper specks across the page.

    What a dusty platen or a cheap photocopier produces. Kept as isolated
    pixels: a speck the size of a bubble would be a different test.
    """
    generator = np.random.default_rng(seed)
    result = image.copy()
    draw = generator.random(image.shape)
    result[draw < density / 2.0] = _INK
    result[draw > 1.0 - density / 2.0] = _PAPER
    return result


def _add_scanner_streak(
    image: NDArray[np.uint8], strength: float, seed: int
) -> NDArray[np.uint8]:
    """Darken one thin band across the page, as a dirty scanner roller does."""
    generator = np.random.default_rng(seed)
    height, width = image.shape[:2]
    vertical = bool(generator.integers(0, 2))
    thickness = max(2, round((width if vertical else height) * 0.004))
    values = image.astype(np.float32)

    if vertical:
        left = int(generator.integers(0, max(width - thickness, 1)))
        values[:, left : left + thickness] *= 1.0 - strength
    else:
        top = int(generator.integers(0, max(height - thickness, 1)))
        values[top : top + thickness, :] *= 1.0 - strength
    streaked: NDArray[np.uint8] = np.clip(values, 0, 255).astype(np.uint8)
    return streaked


def _add_edge_shadow(image: NDArray[np.uint8], strength: float) -> NDArray[np.uint8]:
    """Darken one edge, the way a lifted page shades under a platen lid."""
    width = image.shape[1]
    ramp = np.linspace(1.0 - strength, 1.0, width, dtype=np.float32)
    values = image.astype(np.float32) * ramp[np.newaxis, :]
    shaded: NDArray[np.uint8] = np.clip(values, 0, 255).astype(np.uint8)
    return shaded


def _apply_illumination_gradient(
    image: NDArray[np.uint8], strength: float
) -> NDArray[np.uint8]:
    """Darken the image along its diagonal, as an uneven platen would."""
    height, width = image.shape[:2]
    ramp_y = np.linspace(0.0, 1.0, height, dtype=np.float64)[:, None]
    ramp_x = np.linspace(0.0, 1.0, width, dtype=np.float64)[None, :]
    factor = 1.0 - strength * (ramp_x + ramp_y) / 2.0
    return np.clip(image.astype(np.float64) * factor, 0, 255).astype(np.uint8)


def _draw_filled_rectangle(
    image: NDArray[np.uint8], center: Point, size: tuple[float, float], grey: int = _INK
) -> None:
    """Draw a solid rectangle centred on ``center``, black unless told otherwise."""
    half_width, half_height = size[0] / 2.0, size[1] / 2.0
    cv2.rectangle(
        image,
        (round(center.x - half_width), round(center.y - half_height)),
        (round(center.x + half_width), round(center.y + half_height)),
        grey,
        thickness=cv2.FILLED,
    )


_FAINT_MARKER_GREY = 120
"""Grey level of a deliberately faint registration marker.

Pale enough to challenge a detector that assumes solid black, dark enough that
a human would still call it a printed marker - which is the case worth testing,
because a photocopied sheet produces exactly this."""

_DAMAGE_RATIO = 0.45
"""How much of a damaged marker's width and height is torn away."""


def _damage_rectangle(
    image: NDArray[np.uint8], center: Point, size: tuple[float, float]
) -> None:
    """Punch a paper-coloured notch out of one corner of a marker.

    A torn or scuffed corner is what real damage looks like: the shape is still
    mostly there, so a detector finds *something*, and whether the something is
    still usable is exactly the question.
    """
    half_width, half_height = size[0] / 2.0, size[1] / 2.0
    cv2.rectangle(
        image,
        (round(center.x), round(center.y)),
        (
            round(center.x + half_width * _DAMAGE_RATIO * 2),
            round(center.y + half_height * _DAMAGE_RATIO * 2),
        ),
        _PAPER,
        thickness=cv2.FILLED,
    )


def _draw_text_bars(image: NDArray[np.uint8], spec: SyntheticSheetSpec) -> None:
    """Draw long thin filled bars standing in for lines of printed instructions."""
    bar_height = max(2, round(0.005 * spec.height))
    for index in range(6):
        top = round((0.60 + index * 0.02) * spec.height)
        left = round(0.55 * spec.width)
        right = round((0.88 - 0.04 * (index % 3)) * spec.width)
        cv2.rectangle(image, (left, top), (right, top + bar_height), _INK, thickness=cv2.FILLED)


def _draw_bubbles(image: NDArray[np.uint8], spec: SyntheticSheetSpec) -> None:
    """Draw a grid of unfilled answer bubbles."""
    radius = max(3, round(0.009 * spec.width))
    for row in range(10):
        for column in range(4):
            x = round((0.12 + column * 0.035) * spec.width)
            y = round((0.58 + row * 0.018) * spec.height)
            cv2.circle(image, (x, y), radius, _INK, thickness=_LINE_THICKNESS_PX)


_PRINTED_SYMBOL_GREY = 150
"""Grey level of the symbol printed inside a bubble.

Light, the way real sheets print an option letter - dark enough to be ink, far
lighter than a pencil mark. The gap between the two is precisely what
:func:`omr_scanner.imaging.metrics.estimate_ink_level` exists to find, so
rendering it at solid black would make a measurement test pass for the wrong
reason."""


def _draw_answer_bubbles(image: NDArray[np.uint8], spec: SyntheticSheetSpec) -> None:
    """Draw every configured answer bubble: ring, printed symbol, and any mark.

    A mark is solid black over a concentric ellipse sized so that the fraction
    of the *measured* interior it covers equals
    :attr:`AnswerBubbleSpec.fill`. The sample's own radius comes from
    :class:`~omr_scanner.imaging.metrics.BubbleMetricsConfig` rather than a
    second copy of the number, so the renderer and the measurer cannot drift
    apart and silently change what ``fill=0.35`` means.
    """
    sample_ratio = BubbleMetricsConfig().sample_radius_ratio

    for bubble in spec.answer_bubbles:
        center = spec.to_pixels(bubble.center)
        half_x = bubble.width * spec.width / 2.0
        half_y = bubble.height * spec.height / 2.0
        axes = (max(2, round(half_x)), max(2, round(half_y)))
        position = (round(center.x), round(center.y))

        if bubble.fill > 0.0:
            _draw_mark(
                image,
                bubble,
                center=(center.x, center.y),
                half_x=half_x,
                half_y=half_y,
                sample_ratio=sample_ratio,
            )
        elif bubble.symbol:
            # The symbol only matters on an *unmarked* bubble; a mark covers it.
            scale = max(axes) / 14.0
            (text_w, text_h), _baseline = cv2.getTextSize(
                bubble.symbol, cv2.FONT_HERSHEY_SIMPLEX, scale, 1
            )
            cv2.putText(
                image,
                bubble.symbol,
                (position[0] - text_w // 2, position[1] + text_h // 2),
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                _PRINTED_SYMBOL_GREY,
                thickness=1,
                lineType=cv2.LINE_AA,
            )

        cv2.ellipse(image, position, axes, 0, 0, 360, _INK, thickness=_LINE_THICKNESS_PX)


def _draw_mark(
    image: NDArray[np.uint8],
    bubble: AnswerBubbleSpec,
    *,
    center: tuple[float, float],
    half_x: float,
    half_y: float,
    sample_ratio: float,
) -> None:
    """Draw one candidate's mark in the style the specification asks for.

    Split out of :func:`_draw_answer_bubbles` because the geometry of *where*
    bubbles go and the geometry of *what a mark looks like* are two different
    problems, and one function doing both was already the longest in this
    module.
    """
    grey = _mark_grey(bubble.intensity)
    # Area scales with the square of the radius, so covering a fraction `fill`
    # of the sample disc needs a radius of sqrt(fill) times it. A complete mark
    # covers the whole printed bubble, ring included, exactly as a pen does.
    coverage = 1.0 if bubble.fill >= 1.0 else sample_ratio * math.sqrt(bubble.fill)
    scale = coverage * bubble.size_scale
    radius_x = max(1.0, half_x * scale)
    radius_y = max(1.0, half_y * scale)
    position = (
        round(center[0] + bubble.offset_x * half_x),
        round(center[1] + bubble.offset_y * half_y),
    )
    axes = (max(1, round(radius_x)), max(1, round(radius_y)))
    # One stroke width for every line-based style, proportional to the bubble so
    # that a mark looks the same at 150 and 300 dpi.
    stroke = max(1, round(min(half_x, half_y) * 0.45))

    match bubble.style:
        case MarkStyle.FILL:
            cv2.ellipse(image, position, axes, 0, 0, 360, grey, thickness=cv2.FILLED)
        case MarkStyle.RING:
            cv2.ellipse(image, position, axes, 0, 0, 360, grey, thickness=stroke)
        case MarkStyle.DOT:
            spot = (max(1, round(radius_x * 0.35)), max(1, round(radius_y * 0.35)))
            cv2.ellipse(image, position, spot, 0, 0, 360, grey, thickness=cv2.FILLED)
        case MarkStyle.TICK:
            points = [
                (position[0] - axes[0], position[1]),
                (position[0] - axes[0] // 3, position[1] + axes[1]),
                (position[0] + axes[0], position[1] - axes[1]),
            ]
            cv2.polylines(
                image, [np.array(points, dtype=np.int32)], False, grey, stroke, cv2.LINE_AA
            )
        case MarkStyle.CROSS:
            cv2.line(
                image,
                (position[0] - axes[0], position[1] - axes[1]),
                (position[0] + axes[0], position[1] + axes[1]),
                grey,
                stroke,
                cv2.LINE_AA,
            )
            cv2.line(
                image,
                (position[0] - axes[0], position[1] + axes[1]),
                (position[0] + axes[0], position[1] - axes[1]),
                grey,
                stroke,
                cv2.LINE_AA,
            )
        case MarkStyle.STROKE_H:
            cv2.line(
                image,
                (position[0] - axes[0], position[1]),
                (position[0] + axes[0], position[1]),
                grey,
                stroke,
                cv2.LINE_AA,
            )
        case MarkStyle.STROKE_V:
            cv2.line(
                image,
                (position[0], position[1] - axes[1]),
                (position[0], position[1] + axes[1]),
                grey,
                stroke,
                cv2.LINE_AA,
            )
        case MarkStyle.SLASH:
            cv2.line(
                image,
                (position[0] - axes[0], position[1] + axes[1]),
                (position[0] + axes[0], position[1] - axes[1]),
                grey,
                stroke,
                cv2.LINE_AA,
            )
        case MarkStyle.SCRIBBLE:
            # Three passes back and forth, each a little lower: what a hurried
            # candidate's pen actually leaves inside a bubble.
            points = []
            for step in range(4):
                y = position[1] - axes[1] + round(2 * axes[1] * step / 3)
                x = position[0] + (axes[0] if step % 2 else -axes[0])
                points.append((x, y))
            cv2.polylines(
                image, [np.array(points, dtype=np.int32)], False, grey, stroke, cv2.LINE_AA
            )


def _mark_grey(intensity: float) -> int:
    """Return the grey level a mark of ``intensity`` is drawn in."""
    clamped = min(max(intensity, 0.0), 1.0)
    return round(_PAPER - clamped * (_PAPER - _INK))


def _draw_answer_frames(image: NDArray[np.uint8], spec: SyntheticSheetSpec) -> None:
    """Draw hollow rectangles the size of a marker, including near the corners.

    Two of them sit inside corner search regions on purpose: their outline is
    indistinguishable from a registration marker's, so a detector that looks
    only at contour shape picks them, and the interior-ink test is the only
    thing standing between that detector and a wrongly rectified page.
    """
    width = spec.marker_width * spec.width
    height = spec.marker_height * spec.height
    positions = (
        NormalizedPoint(x=0.17, y=0.12),
        NormalizedPoint(x=0.83, y=0.12),
        NormalizedPoint(x=0.50, y=0.40),
        NormalizedPoint(x=0.12, y=0.90),
    )
    for position in positions:
        center = spec.to_pixels(position)
        cv2.rectangle(
            image,
            (round(center.x - width / 2), round(center.y - height / 2)),
            (round(center.x + width / 2), round(center.y + height / 2)),
            _INK,
            thickness=_LINE_THICKNESS_PX,
        )
