"""Tests for the local-registration probe.

Scope:
    :mod:`omr_scanner.imaging.page_geometry` measures pixels and returns
    numbers. It knows nothing about templates or verdicts, so it is tested here
    against lattices built in the test itself - full control over pitch,
    separation and deformation, and no dependence on marker detection, a
    template document or a rendered sheet.

The property these tests exist to pin down:
    A **projective** distortion must produce a near-zero non-projective
    residual, and a **non-projective** one must not. That distinction is the
    entire reason the module exists: the alignment engine already corrects
    rotation, skew, scale and perspective, so a check that fired on those would
    report every tilted scan and be switched off within a day.

Why the deformations here are not :class:`~omr_scanner.imaging.synthetic.DistortionSpec`:
    Every field of that class is projective or photometric, which is precisely
    what a homography undoes. Using one as a positive case would only prove the
    probe was broken. The positive cases use
    :func:`~omr_scanner.imaging.synthetic.apply_local_warp`, which is
    non-projective by construction.
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from omr_scanner.imaging.page_geometry import (
    LocalRegistrationConfig,
    ProbeFeature,
    ProbeSite,
    SiteMeasurement,
    measure_local_registration,
)
from omr_scanner.imaging.synthetic import (
    LocalWarpSpec,
    apply_local_warp,
    local_warp_displacement,
)

PAGE_WIDTH = 900
PAGE_HEIGHT = 1200
PITCH = 40.0
"""Feature pitch of the test lattice, in pixels."""

RADIUS = 13.0
"""Printed feature radius. Deliberately well short of half the pitch: a lattice
whose features nearly touch carries almost no positional information along the
axis they touch on, which is a real limitation the probe reports rather than
guesses through - see :func:`test_a_lattice_too_tight_to_localise_is_reported_unmatched`."""


def build_lattice_page(
    *, width: int = PAGE_WIDTH, height: int = PAGE_HEIGHT, radius: float = RADIUS
) -> NDArray[np.uint8]:
    """Render a page of printed discs on paper, at :data:`PITCH`."""
    page: NDArray[np.uint8] = np.full((height, width), 245, dtype=np.uint8)
    for center_y, center_x in _lattice_centers(width, height):
        cv2.circle(
            page, (int(center_x), int(center_y)), int(radius), 40, cv2.FILLED, cv2.LINE_AA
        )
    return page


def _lattice_centers(width: int, height: int) -> list[tuple[float, float]]:
    """Every feature centre on the test page, as ``(y, x)``."""
    return [
        (y, x)
        for y in np.arange(PITCH, height - PITCH, PITCH)
        for x in np.arange(PITCH, width - PITCH, PITCH)
    ]


def build_sites(
    *, width: int = PAGE_WIDTH, height: int = PAGE_HEIGHT, block: int = 4
) -> tuple[ProbeSite, ...]:
    """Probe sites covering the test lattice in ``block`` x ``block`` groups."""
    columns = np.arange(PITCH, width - PITCH, PITCH)
    rows = np.arange(PITCH, height - PITCH, PITCH)
    sites: list[ProbeSite] = []
    for row_start in range(0, len(rows) - block + 1, block):
        for column_start in range(0, len(columns) - block + 1, block):
            features = tuple(
                ProbeFeature(
                    center_x=float(columns[column_start + dx]),
                    center_y=float(rows[row_start + dy]),
                    half_width=RADIUS,
                    half_height=RADIUS,
                )
                for dy in range(block)
                for dx in range(block)
            )
            sites.append(
                ProbeSite(
                    site_id=f"{row_start}:{column_start}",
                    group_id=f"band{row_start // block}",
                    center_x=sum(item.center_x for item in features) / len(features),
                    center_y=sum(item.center_y for item in features) / len(features),
                    row_pitch_px=PITCH,
                    column_pitch_px=PITCH,
                    features=features,
                )
            )
    return tuple(sites)


def warp_projective(
    page: NDArray[np.uint8],
    *,
    rotation: float = 0.0,
    scale: float = 1.0,
    translate: tuple[float, float] = (0.0, 0.0),
    perspective: float = 0.0,
) -> NDArray[np.uint8]:
    """Apply a purely projective distortion - the kind a homography undoes."""
    height, width = page.shape
    matrix = np.vstack(
        [cv2.getRotationMatrix2D((width / 2, height / 2), rotation, scale), [0, 0, 1]]
    )
    matrix[0, 2] += translate[0]
    matrix[1, 2] += translate[1]
    if perspective:
        source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
        shift = perspective * min(width, height)
        target = source + np.float32(
            [[shift, shift], [-shift, shift], [-shift, -shift], [shift, -shift]]
        )
        matrix = cv2.getPerspectiveTransform(source, target) @ matrix
    return np.asarray(
        cv2.warpPerspective(
            page, matrix, (width, height), flags=cv2.INTER_CUBIC, borderValue=245
        ),
        dtype=np.uint8,
    )


# ----------------------------------------------------------------------
# A flat page
# ----------------------------------------------------------------------
class TestAnUndistortedPage:
    """The baseline every other case is judged against."""

    def test_every_site_is_located(self) -> None:
        report = measure_local_registration(build_lattice_page(), build_sites())
        assert report.site_count > 8
        assert report.matched_count == report.site_count
        assert report.matched_ratio == 1.0

    def test_displacement_is_negligible(self) -> None:
        report = measure_local_registration(build_lattice_page(), build_sites())
        worst = max(item.displacement_pitch for item in report.measurements)
        assert worst < 0.1, f"a flat page should not move; worst site was {worst:.3f}"

    def test_no_site_saturates(self) -> None:
        report = measure_local_registration(build_lattice_page(), build_sites())
        assert not any(item.saturated for item in report.measurements)

    def test_the_global_refit_succeeds_and_explains_everything(self) -> None:
        report = measure_local_registration(build_lattice_page(), build_sites())
        assert report.refit_succeeded
        assert report.residual_p95_pitch < 0.1

    def test_the_measurement_is_deterministic(self) -> None:
        """Re-reading one sheet must give one answer, byte for byte.

        A batch worker that produced a slightly different residual on a retry
        would make a stored assessment unreproducible, and the feature's whole
        claim is that a flagged sheet can be explained afterwards.
        """
        page = build_lattice_page()
        first = measure_local_registration(page, build_sites())
        second = measure_local_registration(page, build_sites())
        assert [item.dx_px for item in first.measurements] == [
            item.dx_px for item in second.measurements
        ]
        assert first.residual_p95_pitch == second.residual_p95_pitch


# ----------------------------------------------------------------------
# Projective distortion - must be absorbed
# ----------------------------------------------------------------------
class TestProjectiveDistortionIsExplainedAway:
    """The false-positive guard, and the reason the refit exists.

    These magnitudes are deliberately small, because they model what actually
    reaches this probe. A raw scan may be rotated by ten degrees, but
    :func:`~omr_scanner.imaging.alignment.align_sheet` has already corrected
    that by the time a page gets here. What survives alignment is a *residual*
    projective error - a marker detected a pixel late, a sheet very slightly out
    of plane at the clamps - which displaces printing across the whole page
    without the paper having been bent. That is what must be absorbed, and
    measuring it against a ten-degree rotation of an unrectified page would be
    measuring something the pipeline never produces.
    """

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("translation", {"translate": (9.0, -7.0)}),
            ("small rotation", {"rotation": 0.4}),
            ("larger rotation", {"rotation": 1.2}),
            ("scale", {"scale": 0.985}),
            ("perspective", {"perspective": 0.015}),
            ("rotation and perspective", {"rotation": 0.5, "perspective": 0.006}),
        ],
    )
    def test_the_non_projective_residual_all_but_vanishes(
        self, label: str, kwargs: dict[str, float]
    ) -> None:
        page = warp_projective(build_lattice_page(), **kwargs)  # type: ignore[arg-type]
        report = measure_local_registration(page, build_sites())
        assert report.refit_succeeded, label
        assert report.residual_p95_pitch < 0.02, (
            f"{label} is projective and a plane explains it exactly; the refit "
            f"left {report.residual_p95_pitch:.3f} pitch unexplained"
        )

    def test_a_projective_error_can_move_printing_without_being_a_bend(self) -> None:
        """The case that makes the non-projective gate necessary.

        This perspective error displaces the worst probe by more than the
        review threshold - so displacement alone would flag it - while being
        perfectly explained by one better homography. A sheet like this is
        mis-registered, not folded, and the two need different answers.
        """
        page = warp_projective(build_lattice_page(), perspective=0.015)
        report = measure_local_registration(page, build_sites())
        worst = max(item.displacement_pitch for item in report.measurements)
        assert worst > 0.34, "this case is meant to move printing appreciably"
        assert report.residual_p95_pitch < 0.02, (
            "...and is meant to be entirely explained by a global refit"
        )


# ----------------------------------------------------------------------
# Non-projective distortion - must survive
# ----------------------------------------------------------------------
class TestLocalDeformationIsDetected:
    """The true-positive case: a page that is not a plane."""

    def test_a_local_warp_displaces_the_sites_under_it(self) -> None:
        spec = LocalWarpSpec(
            center_x=0.78, center_y=0.78, radius=0.22, amplitude_px=26.0
        )
        page = apply_local_warp(build_lattice_page(), spec)
        report = measure_local_registration(page, build_sites())
        moved = [
            item
            for item in report.measurements
            if item.matched and item.displacement_pitch >= 0.34
        ]
        assert moved, "a local non-projective warp must move something"

    def test_the_damage_is_localised_to_the_warped_corner(self) -> None:
        spec = LocalWarpSpec(
            center_x=0.78, center_y=0.78, radius=0.22, amplitude_px=26.0
        )
        page = apply_local_warp(build_lattice_page(), spec)
        report = measure_local_registration(page, build_sites())
        moved = [
            item
            for item in report.measurements
            if item.matched and item.displacement_pitch >= 0.34
        ]
        # Every displaced site must lie in the lower-right quadrant, which is
        # where the bump was put. A check that flagged the whole page would
        # tell an operator nothing about where to look.
        assert all(item.center_x > PAGE_WIDTH * 0.5 for item in moved)
        assert all(item.center_y > PAGE_HEIGHT * 0.5 for item in moved)

    def test_an_undamaged_corner_is_left_alone(self) -> None:
        spec = LocalWarpSpec(
            center_x=0.78, center_y=0.78, radius=0.22, amplitude_px=26.0
        )
        page = apply_local_warp(build_lattice_page(), spec)
        report = measure_local_registration(page, build_sites())
        far = [
            item
            for item in report.measurements
            if item.matched
            and item.center_x < PAGE_WIDTH * 0.35
            and item.center_y < PAGE_HEIGHT * 0.35
        ]
        assert far
        assert all(item.displacement_pitch < 0.2 for item in far)

    def test_a_stronger_warp_displaces_more(self) -> None:
        """Monotonicity: the measure must track the physical severity."""
        sites = build_sites()
        page = build_lattice_page()
        worst: list[float] = []
        for amplitude in (0.0, 12.0, 24.0, 36.0):
            spec = LocalWarpSpec(
                center_x=0.78, center_y=0.78, radius=0.22, amplitude_px=amplitude
            )
            deformed = apply_local_warp(page, spec) if amplitude else page
            report = measure_local_registration(deformed, sites)
            worst.append(
                max(
                    (
                        item.displacement_pitch
                        for item in report.measurements
                        if item.matched
                    ),
                    default=0.0,
                )
            )
        assert worst == sorted(worst), f"displacement should grow with the bend: {worst}"


# ----------------------------------------------------------------------
# Anisotropic grids - rows and columns spaced differently
# ----------------------------------------------------------------------
ANISOTROPIC = [
    pytest.param(40.0, 40.0, id="square"),
    pytest.param(30.0, 45.0, id="rows-tighter-1.5x"),
    pytest.param(25.0, 50.0, id="rows-tighter-2x"),
    pytest.param(50.0, 25.0, id="columns-tighter-2x"),
    pytest.param(60.0, 20.0, id="columns-tighter-3x"),
]
"""``(row_pitch, column_pitch)`` in pixels, spanning both directions of
anisotropy.

An OMR grid is routinely non-square - this repository's own synthetic answer
sheet spaces rows 31.6 px apart and columns 49.6 - and the failure these cover
is silent: collapsing the two axes into one pitch does not produce a wrong
displacement, it produces *no* displacement, because the sites are discarded as
ambiguous and the sheet quietly goes unmeasured.
"""


def build_anisotropic_page(
    row_pitch: float,
    column_pitch: float,
    *,
    width: int = PAGE_WIDTH,
    height: int = PAGE_HEIGHT,
) -> NDArray[np.uint8]:
    """Render a lattice whose two axes are spaced differently."""
    page: NDArray[np.uint8] = np.full((height, width), 245, dtype=np.uint8)
    half_w = int(min(11.0, column_pitch / 2 - 4))
    half_h = int(min(9.0, row_pitch / 2 - 4))
    for center_y in np.arange(row_pitch, height - row_pitch, row_pitch):
        for center_x in np.arange(column_pitch, width - column_pitch, column_pitch):
            cv2.ellipse(
                page,
                (int(center_x), int(center_y)),
                (half_w, half_h),
                0.0,
                0.0,
                360.0,
                40,
                cv2.FILLED,
                cv2.LINE_AA,
            )
    return page


def build_anisotropic_sites(
    row_pitch: float,
    column_pitch: float,
    *,
    width: int = PAGE_WIDTH,
    height: int = PAGE_HEIGHT,
    block: int = 4,
) -> tuple[ProbeSite, ...]:
    """Probe sites matching :func:`build_anisotropic_page`."""
    half_w = min(11.0, column_pitch / 2 - 4)
    half_h = min(9.0, row_pitch / 2 - 4)
    columns = np.arange(column_pitch, width - column_pitch, column_pitch)
    rows = np.arange(row_pitch, height - row_pitch, row_pitch)
    sites: list[ProbeSite] = []
    for row_start in range(0, len(rows) - block + 1, block):
        for column_start in range(0, len(columns) - block + 1, block):
            features = tuple(
                ProbeFeature(
                    center_x=float(columns[column_start + dx]),
                    center_y=float(rows[row_start + dy]),
                    half_width=half_w,
                    half_height=half_h,
                )
                for dy in range(block)
                for dx in range(block)
            )
            sites.append(
                ProbeSite(
                    site_id=f"{row_start}:{column_start}",
                    group_id=f"band{row_start // block}",
                    center_x=sum(item.center_x for item in features) / len(features),
                    center_y=sum(item.center_y for item in features) / len(features),
                    row_pitch_px=row_pitch,
                    column_pitch_px=column_pitch,
                    features=features,
                )
            )
    return tuple(sites)


class TestAnisotropicGrids:
    """Rows and columns must be measured against their own pitch.

    The regression these lock down: an earlier build scaled the working image by
    a single pitch, so the looser axis kept a longer period than the isotropic
    search window and peak-exclusion radius assumed. The true correlation peak's
    own shoulder then fell outside the exclusion zone, was counted as a rival,
    and the site was thrown away as ambiguous - 12 of 40 probes discarded on a
    flat page, leaving the sheet *unmeasured* rather than mis-measured.
    """

    @pytest.mark.parametrize(("row_pitch", "column_pitch"), ANISOTROPIC)
    def test_a_flat_anisotropic_page_is_fully_matched(
        self, row_pitch: float, column_pitch: float
    ) -> None:
        report = measure_local_registration(
            build_anisotropic_page(row_pitch, column_pitch),
            build_anisotropic_sites(row_pitch, column_pitch),
        )
        assert report.site_count >= 8
        assert report.matched_ratio == 1.0, (
            f"{report.matched_count}/{report.site_count} matched at "
            f"row/column pitch {row_pitch}/{column_pitch}"
        )

    @pytest.mark.parametrize(("row_pitch", "column_pitch"), ANISOTROPIC)
    def test_a_flat_anisotropic_page_shows_no_deformation(
        self, row_pitch: float, column_pitch: float
    ) -> None:
        report = measure_local_registration(
            build_anisotropic_page(row_pitch, column_pitch),
            build_anisotropic_sites(row_pitch, column_pitch),
        )
        worst = max(item.displacement_pitch for item in report.measurements)
        assert worst < 0.2, f"flat page reported {worst:.3f} pitch of movement"
        assert report.residual_p95_pitch < 0.12

    @pytest.mark.parametrize(("row_pitch", "column_pitch"), ANISOTROPIC)
    def test_local_deformation_is_still_detected(
        self, row_pitch: float, column_pitch: float
    ) -> None:
        """Sensitivity must survive the fix, not only the false positives."""
        page = build_anisotropic_page(row_pitch, column_pitch)
        sites = build_anisotropic_sites(row_pitch, column_pitch)
        spec = LocalWarpSpec(
            center_x=0.78,
            center_y=0.78,
            radius=0.20,
            amplitude_px=0.7 * min(row_pitch, column_pitch),
        )
        report = measure_local_registration(apply_local_warp(page, spec), sites)
        moved = [
            item
            for item in report.measurements
            if item.matched and item.displacement_pitch >= 0.34
        ]
        assert moved, "a local non-projective warp must still move something"
        assert all(item.center_x > PAGE_WIDTH * 0.45 for item in moved)
        assert all(item.center_y > PAGE_HEIGHT * 0.45 for item in moved)

    def test_displacement_is_measured_against_each_axis(self) -> None:
        """Half a row pitch is as wrong as half a column pitch.

        However unequal those two distances happen to be in pixels - which is
        the only scale-free way to say "the sample window has moved onto its
        neighbour" on a grid that is not square.
        """
        measurement = SiteMeasurement(
            site_id="s",
            group_id="g",
            center_x=0.0,
            center_y=0.0,
            row_pitch_px=20.0,
            column_pitch_px=60.0,
            dx_px=30.0,
            dy_px=0.0,
            matched=True,
        )
        assert measurement.displacement_pitch == pytest.approx(0.5)
        vertical = dataclasses.replace(measurement, dx_px=0.0, dy_px=10.0)
        assert vertical.displacement_pitch == pytest.approx(0.5)


# ----------------------------------------------------------------------
# Cases the probe must refuse to guess at
# ----------------------------------------------------------------------
class TestWhatThePageCannotTell:
    """Where the honest answer is "not located", not a number."""

    def test_blank_paper_matches_nothing(self) -> None:
        blank: NDArray[np.uint8] = np.full(
            (PAGE_HEIGHT, PAGE_WIDTH), 245, dtype=np.uint8
        )
        report = measure_local_registration(blank, build_sites())
        assert report.matched_count == 0
        assert report.matched_ratio == 0.0

    def test_an_obliterated_area_is_unmatched_not_displaced(self) -> None:
        """A covered region is a different accident from a moved one.

        This is the distinction §4 of the brief turns on: "there is a large dark
        area here" must never be reported as "the page is bent". The probe says
        it could not find the printing, and leaves the conclusion to the caller.
        """
        page = build_lattice_page()
        page[int(PAGE_HEIGHT * 0.6) :, int(PAGE_WIDTH * 0.6) :] = 20
        report = measure_local_registration(page, build_sites())
        covered = [
            item
            for item in report.measurements
            if item.center_x > PAGE_WIDTH * 0.68 and item.center_y > PAGE_HEIGHT * 0.68
        ]
        assert covered
        assert not any(item.matched for item in covered)

    def test_a_lattice_too_tight_to_localise_is_reported_unmatched(self) -> None:
        """Features that nearly touch carry no position along that axis.

        Measured, not hypothetical: a sheet whose rows sit 31.6 px apart with
        27.3 px bubbles has a 4 px gap that does not survive downsampling, and an
        earlier build reported every site in such a zone as confidently
        displaced on a page that was perfectly flat. The peak-uniqueness test
        exists for this, and this test is what holds it in place.
        """
        page = build_lattice_page(radius=PITCH / 2.0 - 1.0)
        report = measure_local_registration(page, build_sites())
        displaced = [
            item
            for item in report.measurements
            if item.matched and item.displacement_pitch >= 0.34
        ]
        assert not displaced, (
            "an ambiguous lattice must be reported as not located, never as "
            f"displaced; {len(displaced)} site(s) claimed a displacement"
        )

    def test_a_page_smaller_than_the_sites_is_not_guessed_at(self) -> None:
        tiny: NDArray[np.uint8] = np.full((60, 60), 245, dtype=np.uint8)
        report = measure_local_registration(tiny, build_sites())
        assert report.matched_count == 0
        assert not report.refit_succeeded

    def test_no_sites_is_not_an_error(self) -> None:
        report = measure_local_registration(build_lattice_page(), [])
        assert report.site_count == 0
        assert report.matched_ratio == 0.0
        assert not report.refit_succeeded


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
class TestConfiguration:
    """The guards that stop a mis-configured probe reporting nonsense."""

    def test_a_search_window_of_half_a_pitch_is_refused(self) -> None:
        """The aliasing guard, stated as a constraint rather than a comment.

        At half a pitch the neighbouring feature becomes an equally good match.
        Measured on the repository's real sheet, a full-pitch window put 22 of
        26 sites on the alias at exactly +/-1.000 pitch.
        """
        with pytest.raises(ValueError, match="search_pitch"):
            LocalRegistrationConfig(search_pitch=0.5)
        with pytest.raises(ValueError, match="search_pitch"):
            LocalRegistrationConfig(search_pitch=1.0)

    def test_a_usable_search_window_is_accepted(self) -> None:
        assert LocalRegistrationConfig(search_pitch=0.45).search_pitch == 0.45

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("pitch_scale_px", 2.0),
            ("min_confidence", 1.0),
            ("max_ambiguity", 0.0),
            ("min_features", 0),
        ],
    )
    def test_out_of_range_settings_are_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            LocalRegistrationConfig(**{field: value})  # type: ignore[arg-type]

    def test_a_site_with_too_few_features_is_skipped(self) -> None:
        site = ProbeSite(
            site_id="sparse",
            group_id="g",
            center_x=200.0,
            center_y=200.0,
            row_pitch_px=PITCH,
            column_pitch_px=PITCH,
            features=(ProbeFeature(200.0, 200.0, RADIUS, RADIUS),),
        )
        report = measure_local_registration(build_lattice_page(), [site])
        assert report.matched_count == 0


# ----------------------------------------------------------------------
# The synthetic deformation itself
# ----------------------------------------------------------------------
class TestTheLocalWarpHelper:
    """The test tool has to be correct, or every case above is meaningless."""

    def test_the_page_borders_are_left_undisturbed(self) -> None:
        """Because the registration markers live there.

        A warp that moved the markers would make the sheet fail to register,
        and the case being tested - a page that registers perfectly while its
        interior has moved - would never arise.
        """
        spec = LocalWarpSpec(amplitude_px=40.0)
        page = build_lattice_page()
        warped = apply_local_warp(page, spec)
        assert np.array_equal(page[0, :], warped[0, :])
        assert np.array_equal(page[-1, :], warped[-1, :])
        assert np.array_equal(page[:, 0], warped[:, 0])
        assert np.array_equal(page[:, -1], warped[:, -1])

    def test_the_displacement_peaks_at_the_stated_centre(self) -> None:
        spec = LocalWarpSpec(
            center_x=0.5, center_y=0.5, radius=0.2, amplitude_px=30.0
        )
        middle = local_warp_displacement(
            PAGE_WIDTH * 0.5, PAGE_HEIGHT * 0.5, spec, width=PAGE_WIDTH, height=PAGE_HEIGHT
        )
        edge = local_warp_displacement(
            PAGE_WIDTH * 0.05, PAGE_HEIGHT * 0.5, spec, width=PAGE_WIDTH, height=PAGE_HEIGHT
        )
        assert middle > edge
        assert edge < 1.0

    def test_it_is_deterministic(self) -> None:
        spec = LocalWarpSpec(amplitude_px=25.0)
        page = build_lattice_page()
        assert np.array_equal(apply_local_warp(page, spec), apply_local_warp(page, spec))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("radius", 0.0), ("edge_margin", 0.5), ("edge_margin", 0.0)],
    )
    def test_an_impossible_warp_is_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            LocalWarpSpec(**{field: value})  # type: ignore[arg-type]

    def test_a_zero_direction_is_refused(self) -> None:
        with pytest.raises(ValueError, match="zero vector"):
            LocalWarpSpec(direction_x=0.0, direction_y=0.0)
