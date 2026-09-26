"""Local registration measurement: is the rectified page actually flat?

Purpose:
    Measure, at many points across an already-rectified page, how far the
    printed features have moved from where the fitted transform says they
    should be - and, crucially, how much of that movement **no global
    projective transform can explain**.

Why this module has to exist at all:
    :func:`~omr_scanner.imaging.alignment.align_sheet` fits a homography to
    exactly four corner markers. Four correspondences determine a homography
    exactly, so the markers' own reprojection residual is zero *by
    construction* - measured at ``0.0`` px on this repository's real sample
    sheet. It is a measure of numerical conditioning and carries no information
    whatever about whether the paper was flat. A sheet that was folded, curled
    or lifted off the platen still produces four crisp corner markers and a
    perfect-looking fit, while the bubbles in the damaged region sit somewhere
    the template does not expect. ``AlignmentMetrics`` says as much in its own
    docstring: *interior* control points are what measures geometric accuracy.
    This module provides them.

The central idea, and the reason it does not fire on ordinary scans:
    A flat page photographed or scanned at any angle is related to the template
    by a **projective** transform. Rotation, scanner skew, translation, scale
    and genuine perspective are all projective; that is precisely why the
    existing homography corrects them. So measuring raw displacement would flag
    every slightly tilted sheet, which is useless.

    Instead, the measured displacement field is **refitted with a global
    homography**, and only the residual that survives that refit is reported.
    Any distortion a plane could have produced is absorbed by the refit and
    contributes nothing. What survives is non-projective: the page was not a
    plane. That is the signature of a bend, and essentially nothing else
    produces it.

How a single point is located:
    Around each site, the expected printed features (bubble outlines) are
    rendered as a small synthetic lattice, and that lattice is located in the
    observed page by normalised cross-correlation
    (:func:`cv2.matchTemplate` with ``TM_CCOEFF_NORMED``). Normalised
    correlation is used rather than a centroid because it is invariant to how
    much of the block the candidate filled in: a heavily marked answer block
    and an empty one have very different ink totals but the same lattice
    *structure*, and it is the structure that carries the geometry.

    The peak is refined to sub-pixel accuracy by a parabolic fit through its
    immediate neighbours, and the peak height is kept as a per-site confidence
    so that a site which could not be located is discarded rather than
    believed.

What does NOT belong here:
    Templates, zones, thresholds and verdicts. This module measures and returns
    numbers; :mod:`omr_scanner.services.scan_quality` decides what they mean,
    exactly as :mod:`omr_scanner.imaging.alignment` measures and
    :mod:`omr_scanner.services.alignment_service` supplies the template. The
    probe sites arrive as plain geometry so that this module stays testable
    with synthetic pages that have no template at all.

Cost:
    Everything runs on a page downscaled so that one feature pitch is a small,
    fixed number of pixels (:attr:`LocalRegistrationConfig.pitch_scale_px`),
    which makes the work proportional to the number of sites rather than to
    scan resolution. On the repository's 2480x3508 sample this is a few
    milliseconds. Nothing here keeps state between calls, so it is safe inside
    a multiprocessing worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class ProbeFeature:
    """One printed ellipse expected at a known place on the canonical page.

    Attributes:
        center_x: Centre on the canonical page, in pixels.
        center_y: The same, vertically.
        half_width: Horizontal half-axis of the printed feature, in pixels.
        half_height: Vertical half-axis.
    """

    center_x: float
    center_y: float
    half_width: float
    half_height: float


@dataclass(frozen=True, slots=True)
class ProbeSite:
    """A neighbourhood of printed features whose displacement is measured.

    A *site* rather than a single feature because one bubble is far too little
    to localise reliably - its neighbours look identical. A block of them has a
    structure that correlates sharply, and its finite extent breaks the
    lattice's periodicity, which is what stops the correlation peak landing one
    whole pitch away.

    Attributes:
        site_id: Stable identifier, unique within one call.
        group_id: What the site belongs to - a template zone id, in practice.
            Carried through so that a caller can aggregate residuals per region
            without matching on coordinates.
        center_x: Centre of the site on the canonical page, in pixels.
        center_y: The same, vertically.
        row_pitch_px: Vertical centre-to-centre spacing of the features here,
            in canonical pixels.
        column_pitch_px: Horizontal spacing. **Kept apart from
            :attr:`row_pitch_px` rather than collapsed into one number**, because
            an OMR grid is routinely anisotropic - on this repository's own
            synthetic answer sheet the rows sit 31.6 px apart and the columns
            49.6 px, a ratio of 1.57 - and the two axes are not
            interchangeable. Reducing them to a single pitch resamples the
            lattice unevenly, leaving one axis with a longer period in working
            space than the isotropic search window and peak-exclusion radius
            assume; the true peak's own shoulder then falls outside the
            exclusion zone, is counted as a rival, and the site is thrown away
            as ambiguous. Measured on a 50-by-25 lattice, that discarded 12 of
            40 probes on a flat page.
        features: The printed features expected in this neighbourhood.
    """

    site_id: str
    group_id: str
    center_x: float
    center_y: float
    row_pitch_px: float
    column_pitch_px: float
    features: tuple[ProbeFeature, ...]

    @property
    def pitch_px(self) -> float:
        """The tighter of the two pitches, for a message a person reads.

        Never used to scale anything - that is what the two axes are for.
        """
        return min(self.row_pitch_px, self.column_pitch_px)


@dataclass(frozen=True, slots=True)
class LocalRegistrationConfig:
    """Tunables for the measurement itself, not for the verdict.

    Attributes:
        pitch_scale_px: Feature pitch, in pixels, that the page is resampled to
            before correlating. Fixing the pitch rather than the scale factor is
            what makes the cost and the accuracy the same for a 200 dpi and a
            600 dpi scan of the same sheet.
        search_pitch: How far a site may be displaced and still be found, in
            pitch units. **Must stay below 0.5.** A lattice of identical
            features is periodic, so a displacement of one whole pitch looks
            exactly like no displacement at all; allowing the search to reach
            that far makes the neighbouring feature an equally good explanation
            and the correlation peak lands there. Measured on this repository's
            real sample sheet, a window of one full pitch put 22 of 26 sites on
            the alias at exactly +/-1.000 pitch while the four that stayed inside
            the window read the truth, 0.01 to 0.11. Half a pitch is also the
            point past which the question stops mattering: a bubble sample
            window displaced that far is already reading its neighbour, so
            "0.5" and "1.5" call for the same human response. A peak on the
            window's edge is reported as :attr:`SiteMeasurement.saturated`,
            meaning "at least this far", rather than as a confident number.
        solid_features: Render each expected feature as a filled disc rather
            than as an outline. On by default, and measured rather than assumed:
            a printed OMR bubble is not an empty ring. It usually contains its
            own option letter or digit, and a bubble the candidate marked is
            solid ink. Correlating a hollow ring against that scored 0.14 on the
            repository's real sheet; a filled disc scored 0.48 on the same
            sites, because a disc resembles both a lettered outline and a
            filled-in mark, while a ring resembles neither.
        ring_thickness_ratio: Stroke width used when
            :attr:`solid_features` is off, as a fraction of the feature's mean
            half-axis.
        blur_sigma_ratio: Gaussian blur applied to both the rendered pattern and
            the observed page, as a fraction of the resampled pitch. A little
            blur makes the correlation peak smooth enough to interpolate and
            absorbs the difference between a filled bubble and an outlined one.
            Too much erases the gaps *between* features, and with them the only
            thing that says where one row ends and the next begins: at the
            earlier 0.12 the 4 px gap on a sheet whose rows sit 31.6 px apart
            did not survive downsampling, and every site in that zone reported a
            confident displacement on a page that was flat. The default is set
            with that margin in mind, together with :attr:`pitch_scale_px`.
        min_confidence: Correlation peak below which a site is reported as
            **not located** rather than as located somewhere. This is what keeps
            the two failure modes apart, and the separation is measured rather
            than assumed. On the repository's real sheet a flat page's weakest
            site scores 0.37. Where the printing has been obliterated - a solid
            black rectangle over the identifier block - the affected sites score
            0.00 to 0.31, because there is no lattice left to match and the peak
            is noise. Where the printing is intact but *displaced* by a curl,
            they still score 0.40 and above. So a low score means "this area
            could not be read", which is a different finding from "this area has
            moved", and reporting the first as the second would tell an operator
            the page was bent when in truth something was covering it.
        max_ambiguity: How close the *second-best* match may come to the best
            one before the site is rejected as ambiguous, as a ratio of peak
            heights. A lattice of identical features can correlate almost as
            well one period away as at the truth, and where the features nearly
            touch along an axis the response can be virtually flat along it -
            measured on a synthetic sheet whose rows sit 31.6 px apart with
            27.3 px bubbles, where the 4 px gap does not survive downsampling
            and every site's peak slid to the edge of the search window. A peak
            that is not clearly the best answer is not an answer, and reporting
            one as a displacement invents a bend on a flat page.
        min_features: Fewest features a site needs before it is worth
            correlating at all.
    """

    pitch_scale_px: float = 16.0
    search_pitch: float = 0.42
    min_confidence: float = 0.22
    max_ambiguity: float = 0.92
    solid_features: bool = True
    ring_thickness_ratio: float = 0.34
    blur_sigma_ratio: float = 0.08
    min_features: int = 6

    def __post_init__(self) -> None:
        """Reject a configuration that could not produce a measurement."""
        if self.pitch_scale_px < 4.0:
            raise ValueError("pitch_scale_px must be at least 4 pixels")
        if not 0.0 < self.search_pitch < 0.5:
            raise ValueError(
                "search_pitch must lie in (0, 0.5): at half a feature pitch the "
                "neighbouring feature becomes an equally good match and the "
                "measurement aliases onto it"
            )
        if not -1.0 < self.min_confidence < 1.0:
            raise ValueError("min_confidence must lie in (-1, 1)")
        if not 0.0 < self.max_ambiguity <= 1.0:
            raise ValueError("max_ambiguity must lie in (0, 1]")
        if self.min_features < 1:
            raise ValueError("min_features must be at least 1")


@dataclass(frozen=True, slots=True)
class SiteMeasurement:
    """Where one site actually turned out to be.

    Attributes:
        site_id: Which site, from :attr:`ProbeSite.site_id`.
        group_id: Its group, copied through from :attr:`ProbeSite.group_id`.
        center_x: Where the site was expected, canonically, in pixels.
        center_y: The same, vertically.
        row_pitch_px: The site's vertical feature pitch in canonical pixels.
        column_pitch_px: Its horizontal pitch.
        dx_px: Measured horizontal displacement, canonical pixels. Positive
            means the printing sits to the right of where it was expected.
        dy_px: Measured vertical displacement.
        confidence: Normalised-correlation peak height in ``[-1, 1]``. Around
            ``0.6``-``0.9`` for a clean block; collapses towards zero where the
            printing is absent, destroyed, or so deformed that the lattice no
            longer matches at any offset.
        matched: Whether the site was located well enough to be used - that
            is, whether :attr:`confidence` reached
            :attr:`LocalRegistrationConfig.min_confidence`. A site that is not
            matched contributes no displacement, but the *fact* that it could
            not be found is itself evidence - printing that cannot be located is
            printing whose geometry has not been confirmed - which is why these
            are returned rather than dropped.
        saturated: The peak sat on the edge of the search window, so the true
            displacement is at least this large and possibly larger. Always
            accompanied by a displacement of exactly the search limit.
    """

    site_id: str
    group_id: str
    center_x: float
    center_y: float
    row_pitch_px: float
    column_pitch_px: float
    dx_px: float = 0.0
    dy_px: float = 0.0
    confidence: float = 0.0
    matched: bool = False
    saturated: bool = False

    @property
    def displacement_px(self) -> float:
        """Magnitude of the measured displacement, in canonical pixels."""
        return float(np.hypot(self.dx_px, self.dy_px))

    @property
    def displacement_pitch(self) -> float:
        """The same, in lattice units - each axis against *its own* pitch.

        The quantity that decides whether a sample window still covers the
        bubble it is meant to measure, and the only scale-free way to say so on
        a grid whose rows and columns are spaced differently: half a row pitch
        down is as wrong as half a column pitch across, however unequal those
        two distances are in pixels.
        """
        if self.row_pitch_px <= 0.0 or self.column_pitch_px <= 0.0:
            return 0.0
        return float(
            np.hypot(
                self.dx_px / self.column_pitch_px, self.dy_px / self.row_pitch_px
            )
        )


@dataclass(frozen=True, slots=True)
class SiteResidual:
    """One site's disagreement with the best global projective explanation.

    Attributes:
        site_id: Which site.
        group_id: Its group.
        center_x: Where the site was expected, canonically, in pixels.
        center_y: The same, vertically.
        residual_px: Distance between where the site was measured and where the
            refitted global homography predicts it, in canonical pixels.
        residual_pitch: The same, divided by the site's feature pitch. **This is
            the quantity the verdict is made on.**
        saturated: Copied from the measurement; a saturated site's residual is a
            lower bound.
    """

    site_id: str
    group_id: str
    center_x: float
    center_y: float
    residual_px: float
    residual_pitch: float
    saturated: bool = False


@dataclass(frozen=True, slots=True)
class LocalRegistrationReport:
    """Everything the geometry probe measured about one page.

    Attributes:
        measurements: One entry per requested site, in the order given,
            including the sites that could not be located.
        residuals: Non-projective residuals, one per *matched* site. Empty when
            the global refit could not be performed.
        refit_succeeded: Whether a global homography could be fitted to the
            measured displacement field. ``False`` means too few sites were
            located, and every residual-derived number is meaningless rather
            than good - callers must check this before reading :attr:`
            residual_median_pitch` and its siblings.
        refit_rms_pitch: RMS residual of the refit across the sites it was
            fitted on, in pitch units. Small here together with large individual
            residuals is the exact signature this module exists to detect: the
            page is well explained as a plane *almost* everywhere.
        residual_median_pitch: Median of :attr:`residuals`, in pitch units.
        residual_p95_pitch: Their 95th percentile. A curl affects a minority of
            the page, so this is where it shows first.
        residual_max_pitch: The largest single residual.
        matched_count: How many sites were located.
        elapsed_seconds: Wall-clock duration of the measurement.
    """

    measurements: tuple[SiteMeasurement, ...] = ()
    residuals: tuple[SiteResidual, ...] = ()
    refit_succeeded: bool = False
    refit_rms_pitch: float = 0.0
    residual_median_pitch: float = 0.0
    residual_p95_pitch: float = 0.0
    residual_max_pitch: float = 0.0
    matched_count: int = 0
    elapsed_seconds: float = 0.0

    @property
    def site_count(self) -> int:
        """How many sites were requested."""
        return len(self.measurements)

    @property
    def matched_ratio(self) -> float:
        """Fraction of requested sites that were located, ``0`` when none were."""
        if not self.measurements:
            return 0.0
        return self.matched_count / len(self.measurements)

    def unmatched(self) -> tuple[SiteMeasurement, ...]:
        """The sites that could not be located, in request order."""
        return tuple(item for item in self.measurements if not item.matched)


def measure_local_registration(
    page: NDArray[np.uint8],
    sites: Sequence[ProbeSite],
    *,
    config: LocalRegistrationConfig | None = None,
) -> LocalRegistrationReport:
    """Measure how well ``page`` matches the printing ``sites`` describe.

    Args:
        page: The **already rectified** canonical page, 8-bit, grayscale or
            colour. Read only; the caller's array is never modified.
        sites: Where printing is expected, in canonical pixels.
        config: Measurement tunables; defaults apply when omitted.

    Returns:
        The per-site measurements and, when enough sites were located, the
        non-projective residuals left after a global projective refit.

    Never raises for an unmeasurable page. A blank sheet, a page of the wrong
    size, or a site list that is empty all produce a report whose
    :attr:`LocalRegistrationReport.refit_succeeded` is ``False`` - "not
    measured" is a result this pipeline must be able to carry, because the
    alternative is an exception in a batch worker three hundred sheets into a
    run.
    """
    started = cv2.getTickCount()
    active = config if config is not None else LocalRegistrationConfig()
    gray = _as_gray(page)

    measurements = tuple(_measure_site(gray, site, active) for site in sites)
    matched = [item for item in measurements if item.matched]

    residuals, refit_ok, rms = _non_projective_residuals(matched)
    values = sorted(item.residual_pitch for item in residuals)
    elapsed = (cv2.getTickCount() - started) / cv2.getTickFrequency()

    return LocalRegistrationReport(
        measurements=measurements,
        residuals=residuals,
        refit_succeeded=refit_ok,
        refit_rms_pitch=rms,
        residual_median_pitch=_percentile(values, 0.50),
        residual_p95_pitch=_percentile(values, 0.95),
        residual_max_pitch=values[-1] if values else 0.0,
        matched_count=len(matched),
        elapsed_seconds=float(elapsed),
    )


# ----------------------------------------------------------------------
# One site
# ----------------------------------------------------------------------
def _measure_site(
    gray: NDArray[np.uint8], site: ProbeSite, config: LocalRegistrationConfig
) -> SiteMeasurement:
    """Locate one site's printed lattice in ``gray``."""
    blank = SiteMeasurement(
        site_id=site.site_id,
        group_id=site.group_id,
        center_x=site.center_x,
        center_y=site.center_y,
        row_pitch_px=site.row_pitch_px,
        column_pitch_px=site.column_pitch_px,
    )
    if (
        len(site.features) < config.min_features
        or site.row_pitch_px <= 0.0
        or site.column_pitch_px <= 0.0
    ):
        return blank

    # One scale per axis, so that *both* lattice periods become
    # `pitch_scale_px` in working space. Normalising the lattice to isotropic
    # here is what lets everything downstream - the square search window, the
    # circular peak-exclusion radius, the single blur sigma - stay isotropic
    # and still be correct on a grid whose rows and columns are spaced
    # differently.
    scale_x = config.pitch_scale_px / site.column_pitch_px
    scale_y = config.pitch_scale_px / site.row_pitch_px
    expected, origin = _render_expected(site, scale_x, scale_y, config)
    if expected is None or origin is None:
        return blank

    search_px = max(2, round(config.search_pitch * config.pitch_scale_px))
    patch = _observed_patch(
        gray, origin, expected.shape, scale_x, scale_y, search_px, config
    )
    if patch is None:
        return blank
    observed, zero_x, zero_y = patch

    response: NDArray[np.float32] = np.asarray(
        cv2.matchTemplate(observed, expected, cv2.TM_CCOEFF_NORMED), dtype=np.float32
    )
    _, peak, _, peak_location = cv2.minMaxLoc(response)
    column, row = peak_location

    offset_x, offset_y = _subpixel_peak(response, column, row)
    dx_scaled = (column + offset_x) - zero_x
    dy_scaled = (row + offset_y) - zero_y

    # How far the peak *could* have travelled in each direction, which is the
    # full search window except where the page ran out first.
    reach_x = min(zero_x, response.shape[1] - 1 - zero_x)
    reach_y = min(zero_y, response.shape[0] - 1 - zero_y)
    saturated = (
        abs(dx_scaled) >= max(1.0, float(reach_x)) - 0.5
        or abs(dy_scaled) >= max(1.0, float(reach_y)) - 0.5
    )

    unmatched = SiteMeasurement(
        site_id=site.site_id,
        group_id=site.group_id,
        center_x=site.center_x,
        center_y=site.center_y,
        row_pitch_px=site.row_pitch_px,
        column_pitch_px=site.column_pitch_px,
        confidence=float(peak),
        matched=False,
    )
    if _is_ambiguous(response, column, row, config):
        return unmatched
    if float(peak) < config.min_confidence:
        # The lattice is not there to be found. Reporting the noise peak as a
        # displacement would describe a covered or destroyed region as a moved
        # one; the caller is told the site could not be located instead.
        return unmatched

    return SiteMeasurement(
        site_id=site.site_id,
        group_id=site.group_id,
        center_x=site.center_x,
        center_y=site.center_y,
        row_pitch_px=site.row_pitch_px,
        column_pitch_px=site.column_pitch_px,
        dx_px=float(dx_scaled) / scale_x,
        dy_px=float(dy_scaled) / scale_y,
        confidence=float(peak),
        matched=True,
        saturated=saturated,
    )


def _render_expected(
    site: ProbeSite,
    scale_x: float,
    scale_y: float,
    config: LocalRegistrationConfig,
) -> tuple[NDArray[np.float32] | None, tuple[float, float] | None]:
    """Draw the printing ``site`` expects, at the working scale.

    Returns the rendered patch as ink-positive float, and the canonical
    coordinate its top-left corner corresponds to.
    """
    xs = [feature.center_x for feature in site.features]
    ys = [feature.center_y for feature in site.features]
    pad_x = max(feature.half_width for feature in site.features) * 1.6
    pad_y = max(feature.half_height for feature in site.features) * 1.6

    left, top = min(xs) - pad_x, min(ys) - pad_y
    right, bottom = max(xs) + pad_x, max(ys) + pad_y

    width = round((right - left) * scale_x)
    height = round((bottom - top) * scale_y)
    if width < 6 or height < 6:
        return None, None

    canvas: NDArray[np.float32] = np.zeros((height, width), dtype=np.float32)
    for feature in site.features:
        # A circular bubble becomes an ellipse once the two axes are scaled
        # differently, and rendering it as one is the point: the expected
        # pattern has to match the observed patch, which was resampled the same
        # way.
        axes = (
            max(1, round(feature.half_width * scale_x)),
            max(1, round(feature.half_height * scale_y)),
        )
        mean_axis = (axes[0] + axes[1]) / 2.0
        thickness = (
            cv2.FILLED
            if config.solid_features
            else max(1, round(mean_axis * config.ring_thickness_ratio))
        )
        cv2.ellipse(
            canvas,
            center=(
                round((feature.center_x - left) * scale_x),
                round((feature.center_y - top) * scale_y),
            ),
            axes=axes,
            angle=0.0,
            startAngle=0.0,
            endAngle=360.0,
            color=1.0,
            thickness=thickness,
            lineType=cv2.LINE_AA,
        )
    return _smooth(canvas, config), (left, top)


def _observed_patch(
    gray: NDArray[np.uint8],
    origin: tuple[float, float],
    template_shape: tuple[int, ...],
    scale_x: float,
    scale_y: float,
    search_px: int,
    config: LocalRegistrationConfig,
) -> tuple[NDArray[np.float32], int, int] | None:
    """Cut and resample the region of ``gray`` the site could be hiding in.

    Returns the resampled patch together with the position, in patch pixels, at
    which a *zero* displacement would place the template's top-left corner.

    The search window is **clamped to the page rather than padded**. Invented
    paper would manufacture a confident match against pixels that do not exist,
    so none is added; but refusing the site outright whenever the window merely
    overruns the edge would blind the check along every margin of the sheet -
    and a folded corner is, by definition, at an edge. So the window is allowed
    to be short on a side, the search simply reaches less far that way, and the
    returned origin says where zero displacement now sits. A site whose printing
    itself falls off the page is still refused: that is a coverage question, and
    :func:`~omr_scanner.services.scan_quality.assess_page_geometry` answers it
    from template geometry.
    """
    left, top = origin
    template_h, template_w = int(template_shape[0]), int(template_shape[1])
    page_h, page_w = gray.shape[:2]

    right = left + template_w / scale_x
    bottom = top + template_h / scale_y
    if left < 0.0 or top < 0.0 or right > page_w or bottom > page_h:
        return None

    margin_x = search_px / scale_x
    margin_y = search_px / scale_y
    x0 = max(0, int(np.floor(left - margin_x)))
    y0 = max(0, int(np.floor(top - margin_y)))
    x1 = min(page_w, int(np.ceil(right + margin_x)))
    y1 = min(page_h, int(np.ceil(bottom + margin_y)))

    crop = gray[y0:y1, x0:x1]
    if crop.size == 0:
        return None

    target_w = max(template_w + 1, round((x1 - x0) * scale_x))
    target_h = max(template_h + 1, round((y1 - y0) * scale_y))
    resized = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_AREA)
    ink = (255.0 - resized.astype(np.float32)) / 255.0
    zero_x = round((left - x0) * scale_x)
    zero_y = round((top - y0) * scale_y)
    return _smooth(ink, config), zero_x, zero_y


def _smooth(patch: NDArray[np.float32], config: LocalRegistrationConfig) -> NDArray[np.float32]:
    """Blur ``patch`` at the working scale, so the correlation peak interpolates."""
    sigma = config.blur_sigma_ratio * config.pitch_scale_px
    if sigma <= 0.0:
        return patch
    return np.asarray(cv2.GaussianBlur(patch, (0, 0), sigma), dtype=np.float32)


def _subpixel_peak(
    response: NDArray[np.float32], column: int, row: int
) -> tuple[float, float]:
    """Refine an integer correlation peak by a parabola through its neighbours.

    Returns the sub-pixel offset to add to ``(column, row)``. Zero when the peak
    sits on the border of the response, where there is no neighbour to fit.
    """
    height, width = response.shape[:2]
    if not (0 < column < width - 1 and 0 < row < height - 1):
        return 0.0, 0.0
    return (
        _parabola_vertex(
            float(response[row, column - 1]),
            float(response[row, column]),
            float(response[row, column + 1]),
        ),
        _parabola_vertex(
            float(response[row - 1, column]),
            float(response[row, column]),
            float(response[row + 1, column]),
        ),
    )


def _is_ambiguous(
    response: NDArray[np.float32],
    column: int,
    row: int,
    config: LocalRegistrationConfig,
) -> bool:
    """Whether the correlation peak is too close to its best rival to believe.

    The peak's own neighbourhood is excluded - the samples beside a genuine peak
    are supposed to be nearly as high, that is what being a peak means - and the
    best value anywhere else is compared with it. Two nearly equal peaks mean
    the lattice matches about as well in two places, and neither is evidence.
    """
    peak = float(response[row, column])
    if peak <= 0.0:
        # A non-positive best correlation is not a match to be adjudicated;
        # `min_confidence` deals with it, and a ratio against it is meaningless.
        return False
    exclusion = max(1, round(config.pitch_scale_px * 0.5))
    rest = response.copy()
    top = max(0, row - exclusion)
    left = max(0, column - exclusion)
    rest[top : row + exclusion + 1, left : column + exclusion + 1] = -1.0
    rival = float(rest.max())
    if rival <= 0.0:
        return False
    return rival / peak > config.max_ambiguity


def _parabola_vertex(left: float, middle: float, right: float) -> float:
    """Return the offset of a parabola's vertex from its middle sample."""
    denominator = left - 2.0 * middle + right
    if abs(denominator) < 1e-12:
        return 0.0
    offset = 0.5 * (left - right) / denominator
    return float(np.clip(offset, -1.0, 1.0))


# ----------------------------------------------------------------------
# The global refit - what separates a bent page from a tilted one
# ----------------------------------------------------------------------
def _non_projective_residuals(
    matched: Sequence[SiteMeasurement],
) -> tuple[tuple[SiteResidual, ...], bool, float]:
    """Remove the best global projective explanation and return what is left.

    A homography is fitted from the expected site centres to the measured ones
    and each site's remaining error is reported. Any distortion a flat page
    could have produced - rotation, skew, scale, perspective, a slightly
    mis-detected marker - is absorbed here and reported as zero, which is what
    keeps ordinary scans silent.

    The fit is deliberately *robust*: an ordinary least-squares fit to every
    site would be dragged by the damaged region, spreading its error across the
    whole page and hiding where the damage actually is. One trimmed refit,
    driven by a median-absolute-deviation cutoff, keeps the fit on the flat
    majority so that the bent minority stands out. It is deterministic - there
    is no RANSAC and no random seed anywhere, because a batch worker must
    produce the same answer for the same sheet every time.
    """
    if len(matched) < 4:
        return (), False, 0.0

    source = np.array(
        [[item.center_x, item.center_y] for item in matched], dtype=np.float64
    )
    target = np.array(
        [
            [item.center_x + item.dx_px, item.center_y + item.dy_px]
            for item in matched
        ],
        dtype=np.float64,
    )

    matrix = _fit_homography(source, target)
    if matrix is None:
        return (), False, 0.0

    offsets = _reprojection(matrix, source, target)
    lattice = _in_lattice_units(offsets, matched)
    keep = _inlier_mask(lattice)
    if int(keep.sum()) >= 4 and int(keep.sum()) < len(matched):
        refined = _fit_homography(source[keep], target[keep])
        if refined is not None:
            matrix = refined
            offsets = _reprojection(matrix, source, target)
            lattice = _in_lattice_units(offsets, matched)
            keep = _inlier_mask(lattice)

    errors = np.linalg.norm(offsets, axis=1)
    residuals: list[SiteResidual] = []
    for item, error, scaled in zip(matched, errors, lattice, strict=True):
        residuals.append(
            SiteResidual(
                site_id=item.site_id,
                group_id=item.group_id,
                center_x=item.center_x,
                center_y=item.center_y,
                residual_px=float(error),
                residual_pitch=float(scaled),
                saturated=item.saturated,
            )
        )

    fitted = lattice[keep] if int(keep.sum()) else lattice
    rms = float(np.sqrt(np.mean(np.square(fitted)))) if fitted.size else 0.0
    return tuple(residuals), True, rms


def _fit_homography(
    source: NDArray[np.float64], target: NDArray[np.float64]
) -> NDArray[np.float64] | None:
    """Least-squares homography from ``source`` to ``target``, or ``None``."""
    if len(source) < 4:
        return None
    matrix, _ = cv2.findHomography(
        source.reshape(-1, 1, 2), target.reshape(-1, 1, 2), method=0
    )
    if matrix is None or not np.all(np.isfinite(matrix)):
        return None
    return np.asarray(matrix, dtype=np.float64)


def _reprojection(
    matrix: NDArray[np.float64],
    source: NDArray[np.float64],
    target: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Offset **vector** from each mapped source point to its measured target.

    A vector rather than a distance because the two axes are normalised by
    different pitches: collapsing to a scalar here would throw away which way
    the printing moved, and with it the only basis for that normalisation.
    """
    mapped = cv2.perspectiveTransform(source.reshape(-1, 1, 2), matrix).reshape(-1, 2)
    offsets: NDArray[np.float64] = mapped - target
    return np.where(np.isfinite(offsets), offsets, 0.0)


def _in_lattice_units(
    offsets: NDArray[np.float64], matched: Sequence[SiteMeasurement]
) -> NDArray[np.float64]:
    """Express each residual vector against its own site's row/column pitch."""
    columns = np.array(
        [item.column_pitch_px if item.column_pitch_px > 0.0 else 1.0 for item in matched],
        dtype=np.float64,
    )
    rows = np.array(
        [item.row_pitch_px if item.row_pitch_px > 0.0 else 1.0 for item in matched],
        dtype=np.float64,
    )
    scaled: NDArray[np.float64] = np.hypot(
        offsets[:, 0] / columns, offsets[:, 1] / rows
    )
    return scaled


def _inlier_mask(errors: NDArray[np.float64]) -> NDArray[np.bool_]:
    """Mark the sites a robust cutoff considers part of the flat majority."""
    median = float(np.median(errors))
    deviation = float(np.median(np.abs(errors - median)))
    if deviation <= 1e-9:
        # Every site agrees to within numerical noise: there is no majority to
        # separate, so all of them are the majority.
        return np.ones(errors.shape, dtype=np.bool_)
    cutoff = median + 2.5 * 1.4826 * deviation
    mask: NDArray[np.bool_] = errors <= cutoff
    return mask


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _as_gray(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return ``image`` as single-channel 8-bit, without copying when possible."""
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 4:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY), dtype=np.uint8)
    if image.ndim == 3 and image.shape[2] == 3:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)
    raise ValueError(f"Unsupported image shape for geometry probing: {image.shape!r}")


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Return a percentile of an already-sorted sequence, ``0.0`` when empty."""
    if not sorted_values:
        return 0.0
    index = round(fraction * (len(sorted_values) - 1))
    return float(sorted_values[max(0, min(len(sorted_values) - 1, index))])


__all__ = [
    "LocalRegistrationConfig",
    "LocalRegistrationReport",
    "ProbeFeature",
    "ProbeSite",
    "SiteMeasurement",
    "SiteResidual",
    "measure_local_registration",
]
