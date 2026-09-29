"""A real blank form, registered once, so synthetic marks can be laid on it.

Purpose:
    Make it possible to generate a labelled dataset whose *paper is real*. The
    template-rendered mode draws the whole page by arithmetic, which is honest
    about its own limits - clean geometry, even paper, marks made of ellipses -
    and those limits are exactly the ones a recognition threshold most needs
    testing against. This module removes them from everything except the marks:
    the page, the print, the paper texture, the scanner's own noise and the
    illumination all come from a photograph of a genuine blank sheet, and only
    the candidate's ink is synthesised.

Responsibilities:
    * :func:`load_reference_scan` - decode a blank scan, register it against the
      template, and refuse it with a readable reason if it will not register.
    * :func:`render_onto_reference` - warp a mark layer into that scan's own
      pixels and composite it.

What does NOT belong here:
    * A second registration algorithm. Registration is
      :func:`omr_scanner.imaging.align_sheet`, unchanged, configured from the
      template by
      :func:`~omr_scanner.services.alignment_service.alignment_config_from_template`
      - the same call the production Scan stage makes. A generator that
      registered pages its own way could produce a dataset the engine cannot
      read for reasons that are the generator's fault, and nobody would be able
      to tell which.
    * Anything about marks, answers or ground truth. This module is handed a
      finished ink layer and asked where to put it.

Which direction the transform goes, since it is the thing most easily got
backwards:
    :func:`~omr_scanner.imaging.align_sheet` returns ``transform_matrix``,
    which maps **scan pixels onto the canonical page** - that is what
    rectification needs. Generation wants the opposite: it holds marks in
    canonical coordinates and needs them in the scan's. That is
    ``inverse_transform_matrix``, and it is the only one this module uses.

An accidental but welcome property:
    Because the transform comes from a full alignment rather than from four
    raw corner positions, a reference scan that was fed upside down or a
    quarter turn askew is registered correctly - the orientation mark resolves
    it - and the marks still land in the right bubbles. A crooked, rotated or
    perspective-distorted blank scan is a usable reference, not a rejected one.

Resolution policy, stated because it is a real decision and not an oversight:
    The produced image is the reference scan **at its own native resolution**.
    A ``--dpi`` setting describes rendering a page from its physical size in
    millimetres and has no meaning here: the pixels already exist and were
    produced by a real scanner at whatever resolution it was set to. The mark
    layer is therefore rendered at an integer multiple of the template's
    canonical size, chosen so that it is at least as dense as the scan it is
    about to be warped into (:data:`MAX_MARK_LAYER_SCALE` caps it), and marks
    come out as sharp as the paper underneath them rather than upscaled from a
    smaller page.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from omr_scanner.errors import ImagingError, OMRScannerError
from omr_scanner.evaluation.write_in import (
    WriteInPreparation,
    prepare_reference_write_in,
    write_in_rows,
)
from omr_scanner.imaging.alignment import align_sheet
from omr_scanner.imaging.models import warnings_as_strings
from omr_scanner.imaging.synthetic import ColorMode, capture_channels, composite_marks
from omr_scanner.services.alignment_service import (
    alignment_config_from_template,
    load_scan_image,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.imaging.synthetic import MarkLayer

_LOGGER = logging.getLogger(__name__)

MAX_MARK_LAYER_SCALE = 4
"""Ceiling on how far the mark layer is supersampled above canonical size.

Four covers a 600 dpi scan of a template authored at 150 dpi, which is the
widest gap an office scanner produces. The cap exists because the layer is a
full page of ``uint8``: at the canonical A4 size of 1240x1754 a scale of 4 is
already 35 MB, and an uncapped ratio would let one absurd reference scan
exhaust memory on every sheet of a hundred-thousand-sheet run."""


class ReferenceScanError(OMRScannerError):
    """A blank reference scan cannot be used to generate sheets from.

    Its own class rather than an :class:`~omr_scanner.errors.ImagingError`
    because the failure is not the alignment engine's: the engine did exactly
    what it should when handed a page it cannot register. What failed is the
    *choice of file*, which is a generation-time decision a person made and can
    correct, so it carries a message aimed at them.
    """


@dataclass(frozen=True, slots=True)
class ReferenceRegistration:
    """How well the reference scan registered, kept for the dataset's record.

    Recorded rather than merely checked. A dataset generated on a marginally
    registered reference is a dataset whose every sheet inherits that margin,
    and six months later the only way to know is to have written it down.

    Attributes:
        max_reprojection_error_px: Largest distance between a detected marker
            mapped through the fitted transform and its canonical target. With
            four correspondences this measures numerical conditioning, not
            geometric accuracy - see
            :class:`~omr_scanner.imaging.models.AlignmentMetrics`.
        min_marker_score: Weakest of the four corner markers' selection scores.
        orientation_confidence: Evidence for the orientation mark, ``[0, 1]``.
        quarter_turns: Clockwise quarter turns the reference scan was fed at.
            Non-zero is fine; it simply means the sheet went through the
            scanner rotated, and the transform accounts for it.
        warnings: The alignment's non-fatal observations, as plain strings.
    """

    max_reprojection_error_px: float
    min_marker_score: float
    orientation_confidence: float
    quarter_turns: int
    warnings: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        """Return this registration as JSON-safe plain data, for the manifest."""
        return {
            "max_reprojection_error_px": round(self.max_reprojection_error_px, 4),
            "min_marker_score": round(self.min_marker_score, 4),
            "orientation_confidence": round(self.orientation_confidence, 4),
            "quarter_turns": self.quarter_turns,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ReferenceScan:
    """A decoded blank form and the transform that puts marks onto it.

    Built once per generation run and reused for every sheet: registering is
    the expensive part, and the answer is the same every time because the
    reference does not change between sheets. Every field is a plain array or
    value, so this survives being sent to a worker process unchanged.

    Attributes:
        name: The file's name, without its directory. Recorded in the dataset;
            the full path is deliberately not, because a dataset should be
            movable and should not carry somebody's folder layout.
        image: The scan itself, in the channel layout the colour mode asks for.
            Read only - :func:`render_onto_reference` never modifies it.
        canonical_to_scan: 3x3 homography mapping canonical page pixels to this
            scan's pixels.
        canonical_width: Canonical page width the transform maps from.
        canonical_height: Canonical page height the transform maps from.
        mark_layer_scale: Integer supersampling factor for the mark layer; see
            :data:`MAX_MARK_LAYER_SCALE` and the module docstring.
        registration: How well it registered.
        write_in: The write-in boxes located on this scan, and what was
            erased from them; ``None`` when the template has none, or when
            preparing them was turned off. When present, :attr:`image` is the
            scan with those boxes emptied.
    """

    name: str
    image: NDArray[np.uint8]
    canonical_to_scan: NDArray[np.float64]
    canonical_width: int
    canonical_height: int
    mark_layer_scale: int
    registration: ReferenceRegistration
    write_in: WriteInPreparation | None = None

    @property
    def width(self) -> int:
        """Width of the produced images, in pixels."""
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        """Height of the produced images, in pixels."""
        return int(self.image.shape[0])

    @property
    def mark_layer_size(self) -> tuple[int, int]:
        """``(width, height)`` the mark layer must be rendered at."""
        return (
            self.canonical_width * self.mark_layer_scale,
            self.canonical_height * self.mark_layer_scale,
        )

    @property
    def mark_layer_to_scan(self) -> NDArray[np.float64]:
        """Homography from supersampled mark-layer pixels to scan pixels.

        The canonical transform with the supersampling divided back out: a
        point at ``k`` times canonical coordinates is the canonical point
        ``p / k``, so the matrix is ``canonical_to_scan`` composed with
        ``diag(1/k, 1/k, 1)``.
        """
        factor = 1.0 / float(self.mark_layer_scale)
        rescale = np.diag(np.array([factor, factor, 1.0], dtype=np.float64))
        return np.asarray(self.canonical_to_scan @ rescale, dtype=np.float64)

    def describe(self) -> dict[str, Any]:
        """Return this reference as JSON-safe plain data, for the manifest."""
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "channels": 3 if self.image.ndim == 3 else 1,
            "canonical_page": [self.canonical_width, self.canonical_height],
            "mark_layer_scale": self.mark_layer_scale,
            "registration": self.registration.describe(),
            "write_in": self.write_in.describe() if self.write_in is not None else None,
        }


def load_reference_scan(
    path: Path,
    template: OmrTemplate,
    *,
    color_mode: ColorMode = ColorMode.GRAYSCALE,
    max_mark_layer_scale: int = MAX_MARK_LAYER_SCALE,
    prepare_write_in: bool = True,
) -> ReferenceScan:
    """Decode a blank scan and register it against the template.

    Also locates the write-in boxes above the identifier and set-code fields
    and erases any handwriting already in them
    (:func:`~omr_scanner.evaluation.write_in.prepare_reference_write_in`). A
    "blank" reference is often a used script, and the number somebody once
    wrote on it would otherwise sit under - and contradict - every identifier
    this run writes. Done here, once, because the reference is shared by every
    sheet of the run.

    Args:
        path: The blank, unmarked scan of the printed form.
        template: The template that describes that form. Its canonical page
            size and marker geometry are what the scan is registered against,
            so a scan of a *different* form is rejected here rather than
            producing a dataset whose marks land in the margins.
        color_mode: Which channel layout to decode into. ``COLOR`` keeps the
            scan's own colour; the other two read it as grey, because a
            black-and-white dataset is captured in grey and quantised at the
            very end (see :class:`~omr_scanner.imaging.synthetic.ColorMode`).
        max_mark_layer_scale: Ceiling on the mark layer's supersampling.
        prepare_write_in: Locate and empty the write-in boxes. ``False`` keeps
            the scan exactly as decoded, and sheets written onto it fall back
            to write-in boxes derived from the template alone.

    Returns:
        The registered reference, ready to be reused for every sheet.

    Raises:
        ReferenceScanError: The file could not be decoded, or could not be
            registered against this template. Both carry a message naming the
            file and what specifically went wrong, because a generation run
            that fails here has failed before writing anything and the operator
            needs to know which of the two inputs to change.
        ValueError: ``max_mark_layer_scale`` is less than one.
    """
    if max_mark_layer_scale < 1:
        raise ValueError(
            f"max_mark_layer_scale must be at least 1, got {max_mark_layer_scale}"
        )

    try:
        decoded = load_scan_image(path, color=color_mode is ColorMode.COLOR)
    except ImagingError as exc:
        raise ReferenceScanError(
            f"Reference scan '{path}' could not be decoded: {exc}",
            user_message=(
                f"'{path.name}' could not be read as an image. Choose a scanned "
                f"blank sheet in PNG, JPEG or TIFF form."
            ),
        ) from exc

    image = capture_channels(decoded, color_mode)
    config = alignment_config_from_template(template)

    try:
        result = align_sheet(image, config=config)
    except ImagingError as exc:
        # The engine behaved correctly; the file is the problem. Said in the
        # engine's own words, because "INSUFFICIENT_MARKERS" is the difference
        # between "photograph the sheet again" and "this is the wrong template".
        raise ReferenceScanError(
            f"Reference scan '{path}' does not register against template "
            f"'{template.name}' ({exc.code}): {exc}",
            user_message=(
                f"'{path.name}' could not be registered against the template "
                f"'{template.name}': {exc.user_message} A reference sheet must be "
                f"a clean, complete scan of the same form the template describes."
            ),
        ) from exc

    scale = _mark_layer_scale(
        scan_width=result.original_width,
        scan_height=result.original_height,
        canonical_width=config.canonical_width,
        canonical_height=config.canonical_height,
        maximum=max_mark_layer_scale,
    )
    registration = ReferenceRegistration(
        max_reprojection_error_px=result.metrics.max_reprojection_error_px,
        min_marker_score=result.metrics.min_marker_score,
        orientation_confidence=result.metrics.orientation_confidence,
        quarter_turns=result.orientation.quarter_turns,
        warnings=warnings_as_strings(result.warnings),
    )

    _LOGGER.info(
        "Reference scan '%s' registered: %dx%d px, canonical %dx%d, mark layer x%d, "
        "quarter turns %d, weakest marker %.2f, warnings %s",
        path.name,
        result.original_width,
        result.original_height,
        config.canonical_width,
        config.canonical_height,
        scale,
        registration.quarter_turns,
        registration.min_marker_score,
        ", ".join(registration.warnings) or "none",
    )

    canonical_to_scan = np.asarray(result.inverse_transform_matrix, dtype=np.float64)
    preparation: WriteInPreparation | None = None
    rows = write_in_rows(template) if prepare_write_in else ()
    if rows:
        image, preparation = prepare_reference_write_in(
            image,
            canonical_to_scan,
            rows,
            canonical_width=config.canonical_width,
            canonical_height=config.canonical_height,
        )

    return ReferenceScan(
        name=path.name,
        image=image,
        canonical_to_scan=canonical_to_scan,
        canonical_width=config.canonical_width,
        canonical_height=config.canonical_height,
        mark_layer_scale=scale,
        registration=registration,
        write_in=preparation,
    )


def render_onto_reference(
    reference: ReferenceScan, layer: MarkLayer
) -> NDArray[np.uint8]:
    """Return the reference scan with ``layer``'s marks composited onto it.

    Args:
        reference: The registered blank form.
        layer: Marks rendered at :attr:`ReferenceScan.mark_layer_size`.

    Returns:
        A new image the same size and channel layout as the reference scan.
        The reference itself is never modified, so one loaded reference serves
        every sheet of a run.

    Raises:
        ValueError: ``layer`` was rendered at the wrong size. Checked rather
            than tolerated: a mismatch means the caller rendered against a
            different page than the one that was registered, and silently
            warping it anyway would displace every mark by an amount nothing
            downstream could detect.
    """
    expected = reference.mark_layer_size
    actual = (layer.image.shape[1], layer.image.shape[0])
    if actual != expected:
        raise ValueError(
            f"Mark layer is {actual[0]}x{actual[1]} but reference "
            f"'{reference.name}' expects {expected[0]}x{expected[1]}"
        )

    warped = np.asarray(
        cv2.warpPerspective(
            layer.image,
            reference.mark_layer_to_scan,
            (reference.width, reference.height),
            flags=cv2.INTER_AREA if reference.mark_layer_scale > 1 else cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            # Paper white: everywhere the mark layer does not reach, the
            # reference must show through completely untouched.
            borderValue=(255, 255, 255, 255),
        ),
        dtype=np.uint8,
    )
    return composite_marks(reference.image, warped)


def _mark_layer_scale(
    *,
    scan_width: int,
    scan_height: int,
    canonical_width: int,
    canonical_height: int,
    maximum: int,
) -> int:
    """Return how far to supersample the mark layer for this scan.

    At least as dense as the scan it will be warped into, so a mark is limited
    by the paper's resolution rather than by the template's canonical size.
    Rounded up to an integer because a fractional factor buys nothing and makes
    the recorded number harder to reason about later.
    """
    if canonical_width <= 0 or canonical_height <= 0:
        raise ValueError("The canonical page must have positive dimensions")
    needed = max(scan_width / canonical_width, scan_height / canonical_height)
    return max(1, min(maximum, math.ceil(needed)))


__all__ = [
    "MAX_MARK_LAYER_SCALE",
    "ReferenceRegistration",
    "ReferenceScan",
    "ReferenceScanError",
    "load_reference_scan",
    "render_onto_reference",
]
