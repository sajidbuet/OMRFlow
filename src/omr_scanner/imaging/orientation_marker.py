"""Finding the printed orientation mark inside a region the user drew around it.

Purpose:
    Answer one question the rest of ``imaging`` cannot: *"the user has drawn a
    rectangle around the orientation dash on this reference sheet - exactly where
    is it?"*. This is a template-authoring question, asked once per sheet design
    with a person watching, and it is not the question
    :mod:`omr_scanner.imaging.orientation` answers.

Why this is a separate module from ``orientation.py``:
    ``orientation.py`` runs during Phase 1 *alignment*: given four already
    detected corner markers and a candidate homography, it decides which of the
    four quarter-turns the scan was fed in, by rectifying the template's expected
    mark window out of the scan and measuring ink in it. It needs the four
    corners, it needs the template, and it answers "which way up", not "where".

    Here there is no homography, no canonical page and no orientation to resolve:
    there is a raw reference image and a rectangle. Reusing the registration
    marker detector is equally wrong - its filters
    (:class:`~omr_scanner.imaging.config.MarkerDetectionConfig`) are tuned for a
    near-square printed marker and reject a 2:1 dash on aspect ratio alone.

Responsibilities:
    * Crop the caller's ROI, threshold it on its own statistics, and measure
      every dark external contour inside it.
    * Score the candidates on the properties an orientation *dash* actually has -
      darkness, rectangularity, aspect ratio, size relative to the ROI, and
      distance from the ROI centre - and return the best one, or a stated reason
      why none qualified.
    * Translate every reported coordinate out of ROI-local space and back into
      full-image pixels.
    * Optionally write an annotated overlay showing the ROI, every candidate and
      why each was rejected.

What does NOT belong here:
    * Any containment test that rejects a candidate for lying inside the ROI. The
      ROI exists precisely to say "look in here"; a mark fully inside it is the
      expected case, not a suspicious one. The crop is the only containment rule.
    * Knowledge of `.omrt` documents, of normalised coordinates, or of Qt. The
      caller (``services.marker_detection_service``) converts pixels to whatever
      the template stores.

Coordinate convention:
    ``roi`` is given in **source-image pixels** and every returned coordinate is
    in **source-image pixels**. ROI-local coordinates exist only inside this
    module, and :func:`_to_image_box` / :func:`_to_image_point` are the only two
    places they are converted - so a translation can be wrong in one place or in
    none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from omr_scanner.imaging.models import BoundingBox, Point

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class OrientationMarkerConfig:
    """Every tunable number the ROI dash detector uses.

    Collected in one frozen dataclass rather than scattered as literals through
    the detector (and certainly not through GUI code), so a sheet whose
    orientation mark is an unusual shape is re-tuned by constructing one of
    these, and so the defaults are documented where they are used.

    Defaults describe a *dash*: a short, solid, roughly 2:1 horizontal bar, the
    conventional asymmetric orientation mark on an OMR sheet. They are
    deliberately not the registration-square criteria.

    Attributes:
        min_area_fraction: Smallest accepted contour area as a fraction of the
            ROI's area. Rejects speckle and scanner noise.
        max_area_fraction: Largest accepted contour area as a fraction of the
            ROI's area. Rejects a large printed block the user did not mean, and
            a ROI whose *background* thresholded as one blob. Set high enough
            (0.85) that a rectangle drawn snugly around the mark still works -
            how tightly the user drew the box must not change the answer.
        expected_aspect_ratio: The dash's width divided by its height when the
            mark is horizontal. Scoring peaks here.
        min_aspect_ratio: Narrowest accepted ratio. ``1.2`` still admits a nearly
            square mark, so a sheet using a small square rather than a dash is
            found rather than rejected on principle.
        max_aspect_ratio: Widest accepted ratio.
        vertical_marks_allowed: Whether a dash printed *vertically* (a ratio
            below 1) is accepted by measuring its longer side against its shorter
            one. On by default: the mark's orientation on the page is the user's
            choice, not this detector's.
        min_fill_ratio: Smallest accepted ratio of contour area to its own
            bounding-box area. A solid printed bar is close to 1.0; an outline,
            a letter or a table corner is far below.
        min_darkness: Smallest accepted mean darkness of the contour's interior,
            in ``[0, 1]`` where 1 is black. Keeps a pale artefact out.
        min_score: Acceptance floor for the combined score in ``[0, 1]``.
        center_weight_radius: Distance from the ROI centre, as a fraction of half
            the ROI's diagonal, at which the centring term of the score reaches
            zero. Generous, because "the user drew the box roughly around it" is
            the only promise the ROI makes - a mark near the ROI edge is still
            accepted (scenario 2 of the synthetic tests), it just loses to a
            centred one.
        min_contrast: Smallest difference between the ROI's darkest and lightest
            pixel for it to be thresholded at all. Below this the region is
            uniform paper, and Otsu would split its scanner mottling into
            hundreds of meaningless specks.
    """

    min_area_fraction: float = 0.002
    max_area_fraction: float = 0.85
    expected_aspect_ratio: float = 2.0
    min_aspect_ratio: float = 1.2
    max_aspect_ratio: float = 6.0
    vertical_marks_allowed: bool = True
    min_fill_ratio: float = 0.62
    min_darkness: float = 0.25
    min_score: float = 0.45
    center_weight_radius: float = 1.5
    min_contrast: int = 40


@dataclass(frozen=True, slots=True)
class OrientationCandidate:
    """One dark shape inside the ROI, measured and scored.

    Attributes:
        box: Bounding box, in **source-image pixels**.
        center: Centroid, in **source-image pixels**.
        area_px: Contour area in square pixels (ROI and image pixels are the same
            scale, so this needs no translation).
        aspect_ratio: Longer side divided by shorter side when
            ``vertical_marks_allowed``; otherwise width divided by height.
        fill_ratio: Contour area divided by its bounding box's area.
        darkness: Mean interior darkness in ``[0, 1]``, 1 being black.
        score: Combined score in ``[0, 1]``.
        accepted: Whether this candidate passed every filter and the score floor.
        rejection: Empty when accepted; otherwise a short, user-facing reason.
    """

    box: BoundingBox
    center: Point
    area_px: float
    aspect_ratio: float
    fill_ratio: float
    darkness: float
    score: float
    accepted: bool
    rejection: str = ""


@dataclass(frozen=True, slots=True)
class OrientationMarkerDetection:
    """The outcome of one ROI search.

    Attributes:
        found: Whether an acceptable candidate was located.
        box: The accepted mark's bounding box in source-image pixels, or ``None``.
        center: The accepted mark's centroid in source-image pixels, or ``None``.
        score: The accepted candidate's score, or the best rejected score, or
            ``0.0`` when the ROI held nothing at all.
        roi: The search region, echoed back in source-image pixels, so a caller
            drawing a diagnostic overlay never has to remember what it asked for.
        candidates: Every measured candidate, accepted and rejected, best score
            first - the material a calibration session needs when detection
            disagrees with the user.
        reason: Empty when found; otherwise why nothing was accepted.
        debug_image_path: Where the annotated overlay was written, when one was
            requested.
    """

    found: bool
    roi: BoundingBox
    box: BoundingBox | None = None
    center: Point | None = None
    score: float = 0.0
    candidates: tuple[OrientationCandidate, ...] = ()
    reason: str = ""
    debug_image_path: Path | None = None


def detect_orientation_marker(
    image: NDArray[np.uint8],
    *,
    roi: BoundingBox,
    config: OrientationMarkerConfig | None = None,
    debug_path: Path | None = None,
) -> OrientationMarkerDetection:
    """Locate the orientation mark inside ``roi``.

    Args:
        image: The full reference image, grayscale or BGR. Never modified.
        roi: The user's search rectangle, in source-image pixels. Clipped to the
            image before use, so a rectangle dragged past the page edge is a
            smaller search area rather than an error.
        config: Shape and acceptance tuning; dash-shaped defaults when omitted.
        debug_path: When given, an annotated PNG is written there showing the ROI,
            every candidate and the accepted one.

    Returns:
        The outcome. Never raises for "nothing found" - a designer session always
        has a person present who can place the mark by hand, and a reason is far
        more useful to them than an exception.

    Raises:
        ValueError: ``image`` is not a 2-D or 3-D pixel array, or ``roi`` does not
            overlap it at all.
    """
    active = config if config is not None else OrientationMarkerConfig()
    gray = _to_grayscale(image)
    clipped = _clip_to_image(roi, width=gray.shape[1], height=gray.shape[0])
    patch = gray[
        int(clipped.y) : int(clipped.bottom), int(clipped.x) : int(clipped.right)
    ]
    if patch.size == 0:
        raise ValueError("The orientation search region does not overlap the image")

    binary = _binarize_roi(patch, config=active)
    contours, _hierarchy = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    candidates = tuple(
        sorted(
            (
                _measure(contour, patch=patch, roi=clipped, config=active)
                for contour in contours
            ),
            key=lambda candidate: candidate.score,
            reverse=True,
        )
    )

    accepted = next((item for item in candidates if item.accepted), None)
    detection = OrientationMarkerDetection(
        found=accepted is not None,
        roi=clipped,
        box=accepted.box if accepted else None,
        center=accepted.center if accepted else None,
        score=accepted.score if accepted else (candidates[0].score if candidates else 0.0),
        candidates=candidates,
        reason="" if accepted else _failure_reason(candidates, config=active),
    )
    if debug_path is not None:
        written = write_debug_overlay(image, detection=detection, path=debug_path)
        detection = OrientationMarkerDetection(
            found=detection.found,
            roi=detection.roi,
            box=detection.box,
            center=detection.center,
            score=detection.score,
            candidates=detection.candidates,
            reason=detection.reason,
            debug_image_path=written,
        )
    return detection


def write_debug_overlay(
    image: NDArray[np.uint8],
    *,
    detection: OrientationMarkerDetection,
    path: Path,
) -> Path:
    """Write an annotated copy of ``image`` showing what the detector saw.

    The ROI in blue, every rejected candidate in red with its reason, the accepted
    one in green with its score. Intended for a developer or a calibration
    session, never shown in the ordinary GUI - which is why it is an explicit
    call with an explicit path rather than a side effect of detection.

    Args:
        image: The image that was searched.
        detection: What :func:`detect_orientation_marker` returned for it.
        path: Destination ``.png``; parent directories are created.

    Returns:
        ``path``.
    """
    canvas = _to_bgr(image)
    roi = detection.roi
    cv2.rectangle(
        canvas,
        (int(roi.x), int(roi.y)),
        (int(roi.right), int(roi.bottom)),
        (255, 128, 0),
        2,
    )
    for candidate in detection.candidates:
        color = (0, 180, 0) if candidate.accepted else (0, 0, 220)
        box = candidate.box
        cv2.rectangle(
            canvas,
            (int(box.x), int(box.y)),
            (int(box.right), int(box.bottom)),
            color,
            2,
        )
        note = (
            f"{candidate.score:.2f}"
            if candidate.accepted
            else f"{candidate.score:.2f} {candidate.rejection}"
        )
        cv2.putText(
            canvas,
            note,
            (int(box.x), max(12, int(box.y) - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            color,
            1,
            cv2.LINE_AA,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), canvas)
    return path


# ----------------------------------------------------------------------
# Measurement and scoring
# ----------------------------------------------------------------------
def _measure(
    contour: NDArray[Any],
    *,
    patch: NDArray[np.uint8],
    roi: BoundingBox,
    config: OrientationMarkerConfig,
) -> OrientationCandidate:
    """Measure one ROI-local contour and translate it into image coordinates."""
    x, y, width, height = cv2.boundingRect(contour)
    area = float(cv2.contourArea(contour))
    box_area = float(width * height)
    fill_ratio = area / box_area if box_area > 0.0 else 0.0

    raw_ratio = width / height if height > 0 else 0.0
    if config.vertical_marks_allowed and raw_ratio > 0.0:
        aspect_ratio = max(raw_ratio, 1.0 / raw_ratio)
    else:
        aspect_ratio = raw_ratio

    darkness = _interior_darkness(contour, patch=patch)
    roi_area = roi.area
    area_fraction = area / roi_area if roi_area > 0.0 else 0.0

    local_box = BoundingBox(x=float(x), y=float(y), width=float(width), height=float(height))
    center = _centroid(contour, fallback=local_box.center)
    center_offset = _center_offset(center, roi=roi)

    rejection = _rejection_reason(
        area_fraction=area_fraction,
        aspect_ratio=aspect_ratio,
        fill_ratio=fill_ratio,
        darkness=darkness,
        config=config,
    )
    score = (
        0.0
        if rejection
        else _score(
            aspect_ratio=aspect_ratio,
            fill_ratio=fill_ratio,
            darkness=darkness,
            area_fraction=area_fraction,
            center_offset=center_offset,
            config=config,
        )
    )
    if not rejection and score < config.min_score:
        rejection = f"score {score:.2f} below the {config.min_score:.2f} floor"

    return OrientationCandidate(
        box=_to_image_box(local_box, roi=roi),
        center=_to_image_point(center, roi=roi),
        area_px=area,
        aspect_ratio=aspect_ratio,
        fill_ratio=fill_ratio,
        darkness=darkness,
        score=score,
        accepted=not rejection,
        rejection=rejection,
    )


def _score(
    *,
    aspect_ratio: float,
    fill_ratio: float,
    darkness: float,
    area_fraction: float,
    center_offset: float,
    config: OrientationMarkerConfig,
) -> float:
    """Combine a candidate's measurements into one score in ``[0, 1]``.

    A weighted mean rather than a product: one mediocre term (a dash printed at
    1.6:1 on a sheet whose expected ratio is 2:1) should cost a candidate some
    score, not disqualify it, because the hard limits in
    :func:`_rejection_reason` are what disqualify. Shape and solidity dominate,
    because they are what separates a printed bar from a letter; position inside
    the ROI only breaks ties.
    """
    shape = _band_score(aspect_ratio, config.expected_aspect_ratio)
    solidity = _floor_score(fill_ratio, config.min_fill_ratio)
    ink = _floor_score(darkness, config.min_darkness)
    size = _plateau_score(
        area_fraction,
        low=config.min_area_fraction,
        comfortable_low=config.min_area_fraction * 4.0,
        comfortable_high=config.max_area_fraction * 0.5,
        high=config.max_area_fraction,
    )
    centred = max(0.0, 1.0 - center_offset / max(config.center_weight_radius, 1e-6))
    return _clip(
        0.34 * shape + 0.26 * solidity + 0.20 * ink + 0.10 * size + 0.10 * centred
    )


def _rejection_reason(
    *,
    area_fraction: float,
    aspect_ratio: float,
    fill_ratio: float,
    darkness: float,
    config: OrientationMarkerConfig,
) -> str:
    """Return why this candidate cannot be the orientation mark, or ``""``.

    Note what is *not* here: nothing tests whether the candidate lies inside the
    ROI, touches its border, or overlaps anything. The user drew the ROI to say
    "search in here"; a mark fully contained in it is the expected result.
    """
    if area_fraction < config.min_area_fraction:
        return f"too small ({area_fraction:.4f} of the search area)"
    if area_fraction > config.max_area_fraction:
        return f"too large ({area_fraction:.2f} of the search area)"
    if aspect_ratio < config.min_aspect_ratio:
        return f"too square (ratio {aspect_ratio:.2f})"
    if aspect_ratio > config.max_aspect_ratio:
        return f"too elongated (ratio {aspect_ratio:.2f})"
    if fill_ratio < config.min_fill_ratio:
        return f"not solid ({fill_ratio:.2f} of its own box)"
    if darkness < config.min_darkness:
        return f"too pale (darkness {darkness:.2f})"
    return ""


def _failure_reason(
    candidates: tuple[OrientationCandidate, ...], *, config: OrientationMarkerConfig
) -> str:
    """Summarise, for the user, why the search found nothing."""
    if not candidates:
        return "No dark shape was found inside the search region."
    best = candidates[0]
    return (
        f"{len(candidates)} shape(s) found, none dash-like enough: "
        f"best was {best.rejection or f'scored {best.score:.2f}'} "
        f"(acceptance floor {config.min_score:.2f})."
    )


# ----------------------------------------------------------------------
# ROI <-> image coordinate translation - the only two places it happens
# ----------------------------------------------------------------------
def _to_image_box(box: BoundingBox, *, roi: BoundingBox) -> BoundingBox:
    """Translate a ROI-local bounding box into source-image pixels."""
    return BoundingBox(x=roi.x + box.x, y=roi.y + box.y, width=box.width, height=box.height)


def _to_image_point(point: Point, *, roi: BoundingBox) -> Point:
    """Translate a ROI-local point into source-image pixels."""
    return Point(x=roi.x + point.x, y=roi.y + point.y)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _to_grayscale(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return a single-channel view of ``image``, converting a colour one."""
    array = np.asarray(image)
    if array.ndim == 2:
        return np.asarray(array, dtype=np.uint8)
    if array.ndim == 3 and array.shape[2] in (3, 4):
        code = cv2.COLOR_BGRA2GRAY if array.shape[2] == 4 else cv2.COLOR_BGR2GRAY
        return np.asarray(cv2.cvtColor(array, code), dtype=np.uint8)
    raise ValueError("An image must be a 2-D grayscale or 3-D colour pixel array")


def _to_bgr(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return a writable three-channel copy of ``image``, for annotation."""
    array = np.asarray(image)
    if array.ndim == 2:
        return np.asarray(cv2.cvtColor(array, cv2.COLOR_GRAY2BGR), dtype=np.uint8)
    if array.shape[2] == 4:
        return np.asarray(cv2.cvtColor(array, cv2.COLOR_BGRA2BGR), dtype=np.uint8)
    return np.asarray(array.copy(), dtype=np.uint8)


def _clip_to_image(roi: BoundingBox, *, width: int, height: int) -> BoundingBox:
    """Clip ``roi`` to the image, keeping whole pixels."""
    left = max(0.0, min(float(int(roi.x)), float(width)))
    top = max(0.0, min(float(int(roi.y)), float(height)))
    right = max(left, min(float(round(roi.right)), float(width)))
    bottom = max(top, min(float(round(roi.bottom)), float(height)))
    return BoundingBox(x=left, y=top, width=right - left, height=bottom - top)


def _binarize_roi(
    patch: NDArray[np.uint8], *, config: OrientationMarkerConfig
) -> NDArray[np.uint8]:
    """Threshold the ROI on its own statistics; ink becomes 255.

    Otsu over the crop, not the page. A user-drawn orientation ROI is one dark
    mark on paper - a textbook bimodal histogram that Otsu splits exactly - while
    the page-wide binarisation the alignment pipeline uses is dominated by
    hundreds of printed bubbles elsewhere and has no reason to put its threshold
    where this crop needs it.

    Adaptive (local-mean) thresholding is specifically wrong here: inside a ROI
    drawn tightly around the mark, the mark *is* a large share of every local
    window, so the local mean follows it and the mark dissolves into its own
    background. That is a real failure this detector hit on a 130x75 ROI around
    the 86x44 dash of ``examples/ECE-0000.png``, and is why the tightness of the
    user's rectangle must not change the answer.

    Otsu's threshold is used exactly as it computes it, never nudged by a
    constant. A ROI drawn snugly around a solidly printed mark has its ink at one
    sharp grey level, and Otsu can legitimately place the threshold *on* that
    level - so subtracting even a few grey levels first can erase the mark
    entirely. (It did: every synthetic scenario in
    ``tests/integration/test_orientation_marker_detection.py`` found nothing,
    while the noisier real scan still worked, which is exactly the shape of bug a
    synthetic test exists to catch.)

    An all-paper ROI has nothing for Otsu to separate, so it splits the scanner's
    own mottling and every pixel below the noise mean becomes "ink".
    ``min_contrast`` is the guard: below that dynamic range the ROI is reported as
    empty rather than as a field of speckle. The shape filters would reject that
    speckle anyway; refusing it here just keeps the candidate list, and the debug
    overlay, readable.
    """
    if int(patch.max()) - int(patch.min()) < config.min_contrast:
        return np.zeros_like(patch)
    _, thresholded = cv2.threshold(
        patch, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )
    return np.asarray(thresholded, dtype=np.uint8)


def _interior_darkness(contour: NDArray[Any], *, patch: NDArray[np.uint8]) -> float:
    """Mean darkness of the contour's filled interior, in ``[0, 1]``."""
    mask = np.zeros(patch.shape[:2], dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, thickness=cv2.FILLED)
    pixels = patch[mask == 255]
    if pixels.size == 0:
        return 0.0
    return float(1.0 - pixels.mean() / 255.0)


def _centroid(contour: NDArray[Any], *, fallback: Point) -> Point:
    """Contour centroid, falling back to the bounding-box centre for a zero moment."""
    moments = cv2.moments(contour)
    m00 = float(moments["m00"])
    if m00 == 0.0:
        return fallback
    return Point(x=float(moments["m10"]) / m00, y=float(moments["m01"]) / m00)


def _center_offset(local_center: Point, *, roi: BoundingBox) -> float:
    """Distance from the ROI centre, as a fraction of half the ROI's diagonal."""
    half_diagonal = float(np.hypot(roi.width, roi.height)) / 2.0
    if half_diagonal <= 0.0:
        return 0.0
    dx = local_center.x - roi.width / 2.0
    dy = local_center.y - roi.height / 2.0
    return float(np.hypot(dx, dy)) / half_diagonal


def _band_score(value: float, expected: float) -> float:
    """Score a measurement against an expected value; 1.0 at ``expected``."""
    if expected <= 0.0:
        return 0.0
    ratio = value / expected
    return _clip(1.0 - abs(1.0 - ratio))


def _plateau_score(
    value: float, *, low: float, comfortable_low: float, comfortable_high: float, high: float
) -> float:
    """Score 1.0 across a comfortable band, tapering to 0 at the hard limits.

    Used for "the mark is a sensible share of the search region": there is no
    single right answer between "a ROI drawn loosely around it" and "a ROI drawn
    snugly around it", so anything in the comfortable band scores equally and only
    the extremes are penalised. A single expected value would arbitrarily prefer
    one of the user's two equally reasonable habits.
    """
    if value <= low or value >= high:
        return 0.0
    if comfortable_low <= value <= comfortable_high:
        return 1.0
    if value < comfortable_low:
        return _clip((value - low) / max(comfortable_low - low, 1e-9))
    return _clip((high - value) / max(high - comfortable_high, 1e-9))


def _floor_score(value: float, minimum: float) -> float:
    """Score a measurement whose only requirement is "at least ``minimum``"."""
    if minimum >= 1.0:
        return 1.0 if value >= minimum else 0.0
    return _clip((value - minimum) / (1.0 - minimum))


def _clip(value: float) -> float:
    """Clamp a score into ``[0, 1]``."""
    return max(0.0, min(1.0, value))


__all__ = [
    "OrientationCandidate",
    "OrientationMarkerConfig",
    "OrientationMarkerDetection",
    "detect_orientation_marker",
    "write_debug_overlay",
]
