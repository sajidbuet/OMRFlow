"""The boundary between a template document and the page-geometry probe.

Purpose:
    Decide whether one rectified sheet's geometry can still be trusted, and say
    *where* and *why* when it cannot.

Responsibilities:
    * Turn an :class:`~omr_scanner.domain.template.OmrTemplate` into the plain
      probe geometry :mod:`omr_scanner.imaging.page_geometry` measures.
    * Work out how much of each template zone was actually captured by the
      scan, from the alignment's own inverse transform.
    * Apply :class:`~omr_scanner.domain.scan_quality.ScanQualityThresholds` to
      those measurements and produce a
      :class:`~omr_scanner.domain.scan_quality.ScanQualityAssessment`.

What does NOT belong here:
    * Pixel algorithms. Locating printing is
      :mod:`omr_scanner.imaging.page_geometry`; this module only supplies it
      with template geometry and interprets what comes back - exactly the split
      :mod:`omr_scanner.services.alignment_service` already makes between a
      template and the alignment engine.
    * Qt, persistence, or the decision to open a conflict. Recognition attaches
      the assessment to its result; :mod:`omr_scanner.services.conflict_policy`
      decides whether it deserves a human.

Why the probe sites are blocks of bubbles:
    They are the only dense, precisely-located printing a template is
    guaranteed to describe. A zone border may not be printed at all, a logo is
    optional, and the four registration markers are - by the arithmetic in
    :mod:`omr_scanner.imaging.page_geometry` - incapable of revealing a bend.
    The bubble lattice is printed across the whole working area of the sheet at
    a pitch the template states exactly, which is precisely what a local
    registration check needs. No OCR is involved and none is wanted: the letters
    inside the bubbles are treated as ink, never as characters.

What "critical" means here:
    The candidate identifier and the set code, resolved through the *same*
    helpers recognition itself uses
    (:func:`~omr_scanner.recognition.fields.choose_identifier_zone`,
    :func:`~omr_scanner.recognition.fields.choose_set_code_zone`) rather than by
    re-deriving the rule. A sheet whose answers are damaged has lost some
    questions; a sheet whose identifier is damaged has lost its owner, and is
    not a record at all. That asymmetry is the only reason
    :attr:`~omr_scanner.domain.scan_quality.ScanQualityStatus.UNUSABLE` exists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.domain.scan_quality import (
    DEFAULT_THRESHOLDS,
    PageArea,
    ScanQualityAssessment,
    ScanQualityIssue,
    ScanQualityIssueCode,
    ScanQualityStatus,
)
from omr_scanner.domain.template import (
    IgnoredFieldDefinition,
    QuestionBlockFieldDefinition,
)
from omr_scanner.imaging.page_geometry import (
    LocalRegistrationConfig,
    ProbeFeature,
    ProbeSite,
    measure_local_registration,
)
from omr_scanner.recognition.fields import choose_identifier_zone, choose_set_code_zone

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.domain.geometry import NormalizedRect
    from omr_scanner.domain.scan_quality import ScanQualityThresholds
    from omr_scanner.domain.template import BubbleGrid, OmrTemplate, Zone
    from omr_scanner.imaging.page_geometry import (
        LocalRegistrationReport,
        SiteMeasurement,
    )

_LOGGER = logging.getLogger(__name__)

PROBE_BLOCK_ROWS = 4
PROBE_BLOCK_COLUMNS = 4
"""Size of one probe block, in bubbles.

Four by four is a compromise measured rather than guessed. Smaller blocks place
more probes and localise damage more finely, but carry fewer features and
correlate less sharply; larger blocks correlate beautifully and cannot say which
half of themselves moved. On the repository's real 100-question sheet this
yields 33 probes covering every zone, measured in about 11 ms.
"""


def build_probe_sites(
    template: OmrTemplate,
    *,
    block_rows: int = PROBE_BLOCK_ROWS,
    block_columns: int = PROBE_BLOCK_COLUMNS,
) -> tuple[ProbeSite, ...]:
    """Return the probe geometry ``template``'s printing implies.

    Args:
        template: The document whose bubble lattice describes the printing.
        block_rows: Bubble rows per probe block.
        block_columns: Bubble columns per probe block.

    Returns:
        One site per block of every non-ignored zone, in template order. Blocks
        with too few bubbles to correlate are omitted rather than measured
        badly, so a template of tiny zones simply yields fewer probes - which
        :func:`assess_page_geometry` then reports as "not evaluated".
    """
    width = float(template.page.canonical_width_px)
    height = float(template.page.canonical_height_px)
    sites: list[ProbeSite] = []

    for zone in template.zones:
        grid = zone.grid
        if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        rows, columns = zone.field.rows, zone.field.columns
        # Both pitches, carried separately all the way down. An OMR grid is
        # routinely anisotropic - this repository's own synthetic answer sheet
        # spaces its rows 31.6 px apart and its columns 49.6 - and collapsing
        # the two into one number makes the probe resample the lattice
        # unevenly, which costs matched probes rather than accuracy. See
        # :class:`~omr_scanner.imaging.page_geometry.ProbeSite`.
        row_pitch = grid.row_pitch * height
        column_pitch = grid.column_pitch * width
        if row_pitch <= 0.0 or column_pitch <= 0.0:
            continue

        half_width = grid.bubble_size.width * width / 2.0
        half_height = grid.bubble_size.height * height / 2.0

        for first_row in range(0, rows, block_rows):
            for first_column in range(0, columns, block_columns):
                features = tuple(
                    _feature(grid, row, column, width, height, half_width, half_height)
                    for row in range(first_row, min(first_row + block_rows, rows))
                    for column in range(
                        first_column, min(first_column + block_columns, columns)
                    )
                )
                if len(features) < LocalRegistrationConfig().min_features:
                    continue
                sites.append(
                    ProbeSite(
                        site_id=f"{zone.id}:{first_row}:{first_column}",
                        group_id=zone.id,
                        center_x=sum(item.center_x for item in features) / len(features),
                        center_y=sum(item.center_y for item in features) / len(features),
                        row_pitch_px=row_pitch,
                        column_pitch_px=column_pitch,
                        features=features,
                    )
                )
    return tuple(sites)


def _feature(
    grid: BubbleGrid,
    row: int,
    column: int,
    width: float,
    height: float,
    half_width: float,
    half_height: float,
) -> ProbeFeature:
    """Build one expected printed bubble, in canonical pixels."""
    center = grid.bubble_center(row, column)
    return ProbeFeature(
        center_x=float(center.x) * width,
        center_y=float(center.y) * height,
        half_width=half_width,
        half_height=half_height,
    )


def assess_page_geometry(
    page: NDArray[np.uint8],
    template: OmrTemplate,
    *,
    inverse_transform: NDArray[np.float64] | None = None,
    source_size: tuple[int, int] | None = None,
    thresholds: ScanQualityThresholds | None = None,
    probe_config: LocalRegistrationConfig | None = None,
    sites: Sequence[ProbeSite] | None = None,
) -> ScanQualityAssessment:
    """Judge one rectified page's geometry against ``template``.

    Args:
        page: The rectified canonical page, as recognition measured it.
        template: The template it was rectified onto.
        inverse_transform: The 3x3 map from canonical pixels back to the
            original scan's pixels - :attr:`
            ~omr_scanner.imaging.models.AlignmentResult.inverse_transform_matrix`.
            Supplied, coverage of each zone is checked against the scan's own
            bounds; omitted, coverage is assumed complete and only local
            registration is judged.
        source_size: ``(width, height)`` of the original scan, needed with
            ``inverse_transform`` to know where its edges were.
        thresholds: The verdict's tunables; conservative defaults when omitted.
        probe_config: The measurement's tunables; defaults when omitted.
        sites: Pre-built probe geometry, to avoid rebuilding it per sheet in a
            batch. Built from ``template`` when omitted.

    Returns:
        The assessment. Never raises for an unmeasurable page: a blank sheet, a
        template with no usable lattice, or a page of the wrong size all yield
        an assessment whose :attr:`
        ~omr_scanner.domain.scan_quality.ScanQualityAssessment.evaluated` is
        ``False``. A batch of a thousand sheets must not stop because one of
        them could not be probed.
    """
    limits = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    probes = tuple(sites) if sites is not None else build_probe_sites(template)

    coverage, uncovered = _zone_coverage(template, inverse_transform, source_size)

    if len(probes) < limits.min_probes:
        return _unevaluated(
            probes_requested=len(probes),
            coverage=coverage,
            reason=(
                "The template does not describe enough printing to verify the "
                "page geometry."
            ),
            issues=_coverage_issues(template, uncovered, limits),
        )

    report = measure_local_registration(page, probes, config=probe_config)
    return _verdict(template, report, coverage, uncovered, limits)


# ----------------------------------------------------------------------
# Coverage: was the paper even there?
# ----------------------------------------------------------------------
def _zone_coverage(
    template: OmrTemplate,
    inverse_transform: NDArray[np.float64] | None,
    source_size: tuple[int, int] | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return per-zone captured fraction, and the zones that are short of full.

    Each zone's canonical rectangle is mapped **back** through the alignment's
    own inverse transform and tested against the original scan's bounds. This is
    pure geometry: it asks whether the paper that should carry this region was
    inside the image at all, which is a different question from whether that
    part of the image is dark. A torn corner, a sheet fed askew off the edge of
    the platen and a page folded under itself all show up here; a filled-in
    answer block, a signature and a scanner shadow do not, because none of them
    moves the paper outside the frame.
    """
    coverage: dict[str, float] = {}
    short: dict[str, float] = {}
    if inverse_transform is None or source_size is None:
        return coverage, short

    source_width, source_height = source_size
    if source_width <= 0 or source_height <= 0:
        return coverage, short

    width = float(template.page.canonical_width_px)
    height = float(template.page.canonical_height_px)

    for zone in template.zones:
        if isinstance(zone.field, IgnoredFieldDefinition):
            continue
        bounds = zone.bounds
        grid_points = _sample_rect(bounds, width, height)
        try:
            mapped = cv2.perspectiveTransform(
                grid_points.reshape(-1, 1, 2), inverse_transform
            ).reshape(-1, 2)
        except cv2.error:  # pragma: no cover - a degenerate matrix
            continue
        if not np.all(np.isfinite(mapped)):
            continue
        inside = (
            (mapped[:, 0] >= 0.0)
            & (mapped[:, 0] <= float(source_width - 1))
            & (mapped[:, 1] >= 0.0)
            & (mapped[:, 1] <= float(source_height - 1))
        )
        fraction = float(np.count_nonzero(inside)) / float(len(inside))
        coverage[zone.id] = fraction
        if fraction < 1.0:
            short[zone.id] = fraction
    return coverage, short


def _sample_rect(
    bounds: NormalizedRect, width: float, height: float, steps: int = 9
) -> NDArray[np.float32]:
    """Return a grid of canonical points covering one zone's rectangle."""
    left = float(bounds.x) * width
    top = float(bounds.y) * height
    right = left + float(bounds.width) * width
    bottom = top + float(bounds.height) * height
    xs = np.linspace(left, right, steps, dtype=np.float32)
    ys = np.linspace(top, bottom, steps, dtype=np.float32)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    return np.stack([mesh_x.ravel(), mesh_y.ravel()], axis=1).astype(np.float32)


# ----------------------------------------------------------------------
# The verdict
# ----------------------------------------------------------------------
def _verdict(
    template: OmrTemplate,
    report: LocalRegistrationReport,
    coverage: dict[str, float],
    uncovered: dict[str, float],
    limits: ScanQualityThresholds,
) -> ScanQualityAssessment:
    """Turn measurements into findings."""
    affected = [
        item
        for item in report.measurements
        if item.matched
        and (item.saturated or item.displacement_pitch >= limits.review_displacement_pitch)
    ]
    unmatched = list(report.unmatched())
    saturated = [item for item in affected if item.saturated]
    displacements = sorted(
        item.displacement_pitch for item in report.measurements if item.matched
    )

    findings: list[ScanQualityIssue] = []
    findings.extend(_coverage_issues(template, uncovered, limits))
    findings.extend(_unreadable_issues(template, unmatched, limits))

    # Most of the printing could not be found. That is not a pass, and it is
    # not a bend either; it is a page this check could not read. It is reported
    # as such **and sent for review**, because the accident this whole feature
    # exists to catch - a fold - destroys the printing the check needs, so
    # "could not measure" is one of the shapes a folded sheet arrives in. A
    # silent pass there would let exactly the wrong sheet through.
    unreadable_page = report.matched_ratio < limits.min_matched_ratio
    if unreadable_page:
        findings.append(
            ScanQualityIssue(
                code=ScanQualityIssueCode.GEOMETRY_NOT_VERIFIED,
                status=ScanQualityStatus.REVIEW,
                detail=(
                    f"Only {report.matched_count} of {report.site_count} printed "
                    f"areas could be located ({report.matched_ratio:.0%}); the "
                    "page geometry could not be confirmed."
                ),
                areas=_areas_of(template, unmatched),
                metrics={
                    "matched_ratio": _round(report.matched_ratio),
                    "matched_sites": float(report.matched_count),
                    "probe_sites": float(report.site_count),
                },
            )
        )

    # Two conditions, both required. Enough probes must have moved *and* the
    # movement must be one no flat page could produce. Either alone is a false
    # positive waiting to happen: a global projective error moves every probe
    # without the paper being bent, and one or two moved probes is a mis-match.
    enough_moved = (
        len(affected) >= limits.min_affected_sites
        or len(saturated) >= limits.saturated_sites_for_review
    )
    not_a_plane = report.residual_p95_pitch >= limits.min_nonprojective_pitch
    if not unreadable_page and enough_moved and not_a_plane:
        findings.extend(_distortion_issues(template, report, affected, limits))

    ordered = tuple(sorted(findings, key=lambda item: -item.status.rank))
    status = ScanQualityStatus.worse_of(*(item.status for item in ordered))
    reason = _reason(status, ordered)

    return ScanQualityAssessment(
        status=status,
        issues=ordered,
        probe_count=report.site_count,
        matched_count=report.matched_count,
        affected_count=len(affected),
        unmatched_count=len(unmatched),
        displacement_median_pitch=_round(_percentile(displacements, 0.50)),
        displacement_p95_pitch=_round(_percentile(displacements, 0.95)),
        displacement_max_pitch=_round(displacements[-1] if displacements else 0.0),
        nonprojective_p95_pitch=_round(report.residual_p95_pitch),
        global_fit_rms_pitch=_round(report.refit_rms_pitch),
        coverage_ratio=_round(min(coverage.values()) if coverage else 1.0),
        evaluated=not unreadable_page,
        reason=reason,
    )


def _distortion_issues(
    template: OmrTemplate,
    report: LocalRegistrationReport,
    affected: Sequence[SiteMeasurement],
    limits: ScanQualityThresholds,
) -> list[ScanQualityIssue]:
    """Findings about printing that moved."""
    total = max(1, report.site_count)
    ratio = len(affected) / total
    areas = _areas_of(template, affected)
    metrics = {
        "affected_sites": float(len(affected)),
        "affected_ratio": _round(ratio),
        "displacement_p95_pitch": _round(report.residual_p95_pitch),
        "nonprojective_p95_pitch": _round(report.residual_p95_pitch),
        "global_fit_rms_pitch": _round(report.refit_rms_pitch),
    }

    by_zone = _group(affected)
    zone_ids = tuple(
        zone_id
        for zone_id, items in by_zone.items()
        if len(items) >= limits.region_affected_sites
    )
    zones = [zone for zone in template.zones if zone.id in zone_ids]

    page_wide = ratio >= limits.unusable_affected_ratio
    status = ScanQualityStatus.UNUSABLE if page_wide else ScanQualityStatus.REVIEW
    critical = _critical_ids(template)
    if any(zone_id in critical for zone_id in zone_ids):
        # The identifier or the set code is in the damaged area: whatever the
        # answers say, the script cannot be attributed with confidence.
        status = ScanQualityStatus.UNUSABLE

    issues = [
        ScanQualityIssue(
            code=ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION,
            status=status,
            detail=(
                f"{len(affected)} of {report.site_count} printed areas are "
                "displaced from the template by more than the tolerance, and a "
                "single flat-page transform cannot account for it."
            ),
            areas=areas,
            zone_ids=zone_ids,
            zone_labels=tuple(zone.label for zone in zones),
            question_range=_question_range(zones),
            metrics=metrics,
        )
    ]

    # Name individual regions too, but only when the damage is not page-wide -
    # listing every zone on a sheet that is bent from end to end is noise.
    if not page_wide:
        for zone in zones:
            items = by_zone[zone.id]
            issues.append(
                ScanQualityIssue(
                    code=ScanQualityIssueCode.REGION_REGISTRATION_ERROR,
                    status=(
                        ScanQualityStatus.UNUSABLE
                        if zone.id in critical
                        else ScanQualityStatus.REVIEW
                    ),
                    detail=(
                        f"Local registration failed across {len(items)} probe "
                        f"point(s) in {zone.label}."
                    ),
                    areas=_areas_of(template, items),
                    zone_ids=(zone.id,),
                    zone_labels=(zone.label,),
                    question_range=_question_range([zone]),
                    metrics={"affected_sites": float(len(items))},
                )
            )
    return issues


def _unreadable_issues(
    template: OmrTemplate,
    unmatched: Sequence[SiteMeasurement],
    limits: ScanQualityThresholds,
) -> list[ScanQualityIssue]:
    """Findings about printing that could not be found at all."""
    if not unmatched:
        return []
    critical = _critical_ids(template)
    by_zone = _group(unmatched)
    affected_critical = [
        zone_id
        for zone_id, items in by_zone.items()
        if zone_id in critical and len(items) >= limits.region_affected_sites
    ]
    if not affected_critical:
        return []
    zones = [zone for zone in template.zones if zone.id in affected_critical]
    items = [item for zone in zones for item in by_zone[zone.id]]
    return [
        ScanQualityIssue(
            code=ScanQualityIssueCode.CRITICAL_REGION_UNREADABLE,
            status=ScanQualityStatus.UNUSABLE,
            detail=(
                "The printing that identifies this script could not be located, "
                "so the sheet cannot be attributed to a candidate."
            ),
            areas=_areas_of(template, items),
            zone_ids=tuple(zone.id for zone in zones),
            zone_labels=tuple(zone.label for zone in zones),
            metrics={"unmatched_sites": float(len(items))},
        )
    ]


def _coverage_issues(
    template: OmrTemplate,
    uncovered: dict[str, float],
    limits: ScanQualityThresholds,
) -> list[ScanQualityIssue]:
    """Findings about template area the scan never captured."""
    if not uncovered:
        return []
    critical = _critical_ids(template)
    issues: list[ScanQualityIssue] = []
    missing_zones: list[Zone] = []
    worst = 1.0

    for zone in template.zones:
        fraction = uncovered.get(zone.id)
        if fraction is None:
            continue
        limit = (
            limits.critical_coverage_ratio
            if zone.id in critical
            else limits.answer_coverage_ratio
        )
        if fraction >= limit:
            continue
        missing_zones.append(zone)
        worst = min(worst, fraction)

    if not missing_zones:
        return issues

    critical_hit = any(zone.id in critical for zone in missing_zones)
    issues.append(
        ScanQualityIssue(
            code=ScanQualityIssueCode.PARTIAL_PAGE,
            status=(
                ScanQualityStatus.UNUSABLE if critical_hit else ScanQualityStatus.REVIEW
            ),
            detail=(
                "Part of the sheet was not captured by the scan: "
                f"{', '.join(zone.label for zone in missing_zones)} "
                f"{'is' if len(missing_zones) == 1 else 'are'} only "
                f"{worst:.0%} inside the scanned page."
            ),
            areas=tuple(
                dict.fromkeys(
                    PageArea.containing(
                        float(zone.bounds.x) + float(zone.bounds.width) / 2.0,
                        float(zone.bounds.y) + float(zone.bounds.height) / 2.0,
                    )
                    for zone in missing_zones
                )
            ),
            zone_ids=tuple(zone.id for zone in missing_zones),
            zone_labels=tuple(zone.label for zone in missing_zones),
            question_range=_question_range(missing_zones),
            metrics={"min_coverage_ratio": _round(worst)},
        )
    )
    if critical_hit:
        issues.append(
            ScanQualityIssue(
                code=ScanQualityIssueCode.CRITICAL_REGION_UNREADABLE,
                status=ScanQualityStatus.UNUSABLE,
                detail=(
                    "The region carrying this script's identity was not fully "
                    "captured, so the sheet cannot be attributed."
                ),
                zone_ids=tuple(
                    zone.id for zone in missing_zones if zone.id in critical
                ),
                zone_labels=tuple(
                    zone.label for zone in missing_zones if zone.id in critical
                ),
                metrics={"min_coverage_ratio": _round(worst)},
            )
        )
    return issues


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _critical_ids(template: OmrTemplate) -> frozenset[str]:
    """Zone ids the record cannot do without, via recognition's own rules."""
    chosen = (choose_identifier_zone(template), choose_set_code_zone(template))
    return frozenset(zone.id for zone in chosen if zone is not None)


def _group(items: Sequence[SiteMeasurement]) -> dict[str, list[SiteMeasurement]]:
    """Bucket measurements by the zone they came from, preserving order."""
    grouped: dict[str, list[SiteMeasurement]] = {}
    for item in items:
        grouped.setdefault(item.group_id, []).append(item)
    return grouped


def _areas_of(
    template: OmrTemplate, items: Sequence[SiteMeasurement]
) -> tuple[PageArea, ...]:
    """Coarse page areas the given sites fall in, de-duplicated, in order."""
    width = float(template.page.canonical_width_px)
    height = float(template.page.canonical_height_px)
    return tuple(
        dict.fromkeys(
            PageArea.containing(item.center_x / width, item.center_y / height)
            for item in items
        )
    )


def _question_range(zones: Sequence[Zone]) -> str:
    """Return ``"81-100"`` for the questions ``zones`` carry, or ``""``.

    The single most useful fragment of a review sentence, because it is what an
    invigilator recognises: "the lower-right area" is a place on a page, but
    "questions 81-100" is the part of the examination in doubt.
    """
    numbers: list[int] = []
    for zone in zones:
        field = zone.field
        if isinstance(field, QuestionBlockFieldDefinition):
            numbers.extend((field.first_question, field.last_question))
    if not numbers:
        return ""
    low, high = min(numbers), max(numbers)
    return str(low) if low == high else f"{low}-{high}"


def _reason(status: ScanQualityStatus, issues: Sequence[ScanQualityIssue]) -> str:
    """One short sentence for a queue row."""
    if status is ScanQualityStatus.PASS or not issues:
        return ""
    return issues[0].summary()


def _unevaluated(
    *,
    probes_requested: int,
    coverage: dict[str, float],
    reason: str,
    issues: Sequence[ScanQualityIssue],
) -> ScanQualityAssessment:
    """Build the assessment for a page that could not be judged."""
    ordered = tuple(sorted(issues, key=lambda item: -item.status.rank))
    return ScanQualityAssessment(
        status=ScanQualityStatus.worse_of(*(item.status for item in ordered)),
        issues=ordered,
        probe_count=probes_requested,
        coverage_ratio=_round(min(coverage.values()) if coverage else 1.0),
        evaluated=False,
        reason=_reason(ScanQualityStatus.PASS, ordered) or reason,
    )


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Return a percentile of an already-sorted sequence, ``0.0`` when empty."""
    if not sorted_values:
        return 0.0
    index = round(fraction * (len(sorted_values) - 1))
    return float(sorted_values[max(0, min(len(sorted_values) - 1, index))])


def _round(value: float) -> float:
    """Round a stored metric to four places.

    Stored, compared and shown to people; four places is far beyond the
    measurement's real precision and keeps a persisted assessment from
    differing in its sixteenth decimal between two machines.
    """
    if not np.isfinite(value):
        return 0.0
    return float(round(float(value), 4))


__all__ = [
    "PROBE_BLOCK_COLUMNS",
    "PROBE_BLOCK_ROWS",
    "assess_page_geometry",
    "build_probe_sites",
]
