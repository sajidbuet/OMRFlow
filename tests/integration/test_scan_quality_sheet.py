"""Scan-quality detection measured against the repository's real OMR page.

Scope:
    End-to-end, from an image file to a verdict: ``examples/ECE-0000.png`` read
    with ``examples/templates/ece_0000_sample.omrt``. Both are committed, so
    these tests need nothing from a developer's machine - and in particular
    nothing from ``/Scratch``, which holds real candidate scans and is never
    committed.

Why a real scan rather than a rendered one:
    A drawing of an OMR sheet is made of the same primitives the detector looks
    for, and a check tuned on one can fail on paper. This page carries what
    paper carries: print that is not perfectly black, bubbles with option
    letters inside them, scanner noise, and a registration that already emits
    ``MULTIPLE_CORNER_CANDIDATES``. The numbers quoted in
    :class:`~omr_scanner.domain.scan_quality.ScanQualityThresholds` were
    measured here.

What the two halves of this file prove:
    That an undamaged sheet stays silent under every ordinary indignity a
    scanner inflicts on it, and that a sheet which was physically not flat is
    caught and localised. The first half matters more. A geometry check that
    cries wolf is switched off within a week, and then it catches nothing at
    all - so the passing cases outnumber the failing ones here deliberately.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from omr_scanner.domain.review import ConflictType
from omr_scanner.domain.scan_quality import (
    PageArea,
    ScanQualityIssueCode,
    ScanQualityStatus,
)
from omr_scanner.domain.template import OmrTemplate
from omr_scanner.imaging.alignment import align_sheet
from omr_scanner.imaging.synthetic import LocalWarpSpec, apply_local_warp
from omr_scanner.services.alignment_service import alignment_config_from_template
from omr_scanner.services.conflict_policy import detect_conflicts
from omr_scanner.services.recognition_models import RecognitionOutcome, StatusCode
from omr_scanner.services.recognition_service import recognise_scan
from omr_scanner.services.scan_quality import assess_page_geometry, build_probe_sites

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SHEET = REPOSITORY_ROOT / "examples" / "ECE-0000.png"
SAMPLE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
"""Resolved from this file's own location, never the working directory, so the
tests pass run from anywhere."""

pytestmark = pytest.mark.skipif(
    not SAMPLE_SHEET.exists() or not SAMPLE_TEMPLATE.exists(),
    reason="the committed sample sheet or its template is missing",
)


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    """The template describing the sample sheet."""
    return OmrTemplate.model_validate_json(
        SAMPLE_TEMPLATE.read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def scan() -> NDArray[np.uint8]:
    """The real scan, grayscale. Never mutated; every case warps a copy."""
    image = cv2.imread(str(SAMPLE_SHEET), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"could not read {SAMPLE_SHEET}"
    return np.asarray(image, dtype=np.uint8)


def assess(image: NDArray[np.uint8], template: OmrTemplate):
    """Align ``image`` and judge its geometry, as the pipeline does."""
    alignment = align_sheet(image, config=alignment_config_from_template(template))
    return assess_page_geometry(
        alignment.normalized_image,
        template,
        inverse_transform=alignment.inverse_transform_matrix,
        source_size=(alignment.original_width, alignment.original_height),
        sites=build_probe_sites(template),
    )


def curl(
    image: NDArray[np.uint8],
    *,
    center: tuple[float, float],
    amplitude_px: float,
    radius: float = 0.22,
) -> NDArray[np.uint8]:
    """Bend ``image`` locally, leaving its registration markers alone."""
    return apply_local_warp(
        image,
        LocalWarpSpec(
            center_x=center[0],
            center_y=center[1],
            radius=radius,
            amplitude_px=amplitude_px,
        ),
    )


def projective(
    image: NDArray[np.uint8],
    *,
    rotation: float = 0.0,
    scale: float = 1.0,
    perspective: float = 0.0,
) -> NDArray[np.uint8]:
    """Distort ``image`` the way a flat sheet on a scanner is distorted."""
    padded = cv2.copyMakeBorder(image, 120, 120, 120, 120, cv2.BORDER_CONSTANT, value=235)
    height, width = padded.shape
    matrix = np.vstack(
        [cv2.getRotationMatrix2D((width / 2, height / 2), rotation, scale), [0, 0, 1]]
    )
    if perspective:
        source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
        shift = perspective * min(width, height)
        target = source + np.float32(
            [[shift, shift * 0.4], [-shift, shift], [-shift * 0.5, -shift], [shift, -shift]]
        )
        matrix = cv2.getPerspectiveTransform(source, target) @ matrix
    return np.asarray(
        cv2.warpPerspective(
            padded, matrix, (width, height), flags=cv2.INTER_CUBIC, borderValue=235
        ),
        dtype=np.uint8,
    )


# ----------------------------------------------------------------------
# The half that matters most
# ----------------------------------------------------------------------
class TestAnUndamagedSheetStaysSilent:
    """Every ordinary indignity a scanner inflicts, and none of them a warning."""

    def test_the_sheet_as_scanned_passes(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        assessment = assess(scan, template)
        assert assessment.status is ScanQualityStatus.PASS
        assert assessment.evaluated, "the check must actually have run"
        assert assessment.affected_count == 0
        assert assessment.issues == ()

    def test_every_probe_is_located_on_real_paper(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        assessment = assess(scan, template)
        assert assessment.probe_count >= 8
        assert assessment.matched_count == assessment.probe_count

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("gentle rotation", {"rotation": 2.0}),
            ("scanner skew", {"rotation": 7.0}),
            ("reverse rotation", {"rotation": -8.0}),
            ("scaled down", {"scale": 0.85}),
            ("perspective", {"perspective": 0.02}),
            ("strong perspective", {"perspective": 0.05}),
            ("rotation and perspective", {"rotation": 5.0, "perspective": 0.03}),
        ],
    )
    def test_projective_distortion_is_not_a_geometry_fault(
        self,
        scan: NDArray[np.uint8],
        template: OmrTemplate,
        label: str,
        kwargs: dict[str, float],
    ) -> None:
        """A flat page at an angle is still a flat page.

        These are exactly the distortions the alignment homography exists to
        undo. Flagging them would mean flagging almost every sheet in a real
        batch.
        """
        assessment = assess(projective(scan, **kwargs), template)
        assert assessment.status is ScanQualityStatus.PASS, label
        # Not merely "nothing was flagged": the check has to have *run*. A
        # homography-corrected page must still be fully measurable, or the pass
        # means only that the measurement failed quietly.
        assert assessment.evaluated, label
        assert assessment.is_confirmed_clean, label

    def test_noise_does_not_move_the_printing(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        generator = np.random.default_rng(7)
        noisy = np.clip(
            scan.astype(np.int16) + generator.normal(0.0, 12.0, scan.shape), 0, 255
        ).astype(np.uint8)
        assert assess(noisy, template).status is ScanQualityStatus.PASS

    def test_blur_does_not_move_the_printing(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        assert (
            assess(cv2.GaussianBlur(scan, (5, 5), 1.6), template).status
            is ScanQualityStatus.PASS
        )

    def test_a_dim_exposure_does_not_move_the_printing(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        dim = np.clip(scan.astype(np.int16) * 0.7, 0, 255).astype(np.uint8)
        assert assess(dim, template).status is ScanQualityStatus.PASS

    def test_heavy_handwriting_is_not_a_bend(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        """Ink a candidate added is content, not geometry.

        §4 of the brief in one test: a name, a signature and working-out are
        legitimate dark content, and detecting "a large dark area" and calling
        it a fold is precisely the mistake this feature must not make.
        """
        written = scan.copy()
        generator = np.random.default_rng(1)
        for _ in range(400):
            start = (
                int(generator.integers(300, 2100)),
                int(generator.integers(300, 900)),
            )
            end = (
                start[0] + int(generator.integers(-60, 60)),
                start[1] + int(generator.integers(-30, 30)),
            )
            cv2.line(written, start, end, 30, 5)
        assert assess(written, template).status is ScanQualityStatus.PASS


# ----------------------------------------------------------------------
# The half the feature was asked for
# ----------------------------------------------------------------------
class TestAPhysicallyBentSheetIsCaught:
    """A page that was not flat, and could not have been made flat."""

    def test_a_curled_lower_right_corner_needs_review(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        assessment = assess(
            curl(scan, center=(0.80, 0.83), amplitude_px=50.0), template
        )
        assert assessment.status is ScanQualityStatus.REVIEW
        assert ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION.value in assessment.codes

    def test_it_says_which_part_of_the_page(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        """"Approximately which region is unreliable" - the acceptance criterion."""
        assessment = assess(
            curl(scan, center=(0.80, 0.83), amplitude_px=50.0), template
        )
        issue = assessment.issue(ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION)
        assert issue is not None
        assert PageArea.BOTTOM_RIGHT in issue.areas or PageArea.RIGHT in issue.areas
        assert PageArea.TOP_LEFT not in issue.areas

    def test_it_names_the_questions_in_doubt(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        """The fragment an invigilator actually acts on."""
        assessment = assess(
            curl(scan, center=(0.80, 0.83), amplitude_px=50.0), template
        )
        issue = assessment.issue(ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION)
        assert issue is not None
        assert issue.question_range, "the affected question block should be named"
        assert issue.zone_ids

    def test_a_worse_bend_is_measured_as_worse(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        mild = assess(curl(scan, center=(0.80, 0.83), amplitude_px=35.0), template)
        severe = assess(curl(scan, center=(0.80, 0.83), amplitude_px=70.0), template)
        assert severe.affected_count >= mild.affected_count
        assert severe.nonprojective_p95_pitch > mild.nonprojective_p95_pitch

    def test_damage_over_the_identifier_makes_the_sheet_unusable(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        """A script whose owner cannot be established is not a result.

        The asymmetry that justifies a third status: the same physical accident
        over an answer block costs some questions, and over the roll number
        costs the whole script.
        """
        assessment = assess(
            curl(scan, center=(0.17, 0.33), amplitude_px=70.0, radius=0.14), template
        )
        assert assessment.status is ScanQualityStatus.UNUSABLE

    def test_obliterated_identifier_printing_is_reported_as_unreadable(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        """Covered printing is a different finding from moved printing.

        A solid block over the identifier must not be reported as a bend. The
        page may be perfectly flat; what is wrong is that the printing is not
        visible, and an operator told "the page is bent" would look for the
        wrong thing.
        """
        covered = scan.copy()
        covered[1000:1400, 300:700] = 35
        assessment = assess(covered, template)
        assert assessment.status is ScanQualityStatus.UNUSABLE
        assert (
            ScanQualityIssueCode.CRITICAL_REGION_UNREADABLE.value in assessment.codes
        )
        assert assessment.unmatched_count > 0

    def test_an_undamaged_region_is_not_blamed(
        self, scan: NDArray[np.uint8], template: OmrTemplate
    ) -> None:
        assessment = assess(
            curl(scan, center=(0.80, 0.83), amplitude_px=50.0), template
        )
        blamed = set(assessment.affected_zone_ids)
        assert blamed, "something should be blamed"
        assert "student_id" not in blamed
        assert "set_code" not in blamed


# ----------------------------------------------------------------------
# Through the whole pipeline
# ----------------------------------------------------------------------
class TestThroughRecognition:
    """What a batch actually produces, from a file on disk."""

    @pytest.fixture
    def flat_file(self, scan: NDArray[np.uint8], tmp_path: Path) -> Path:
        path = tmp_path / "flat.png"
        cv2.imwrite(str(path), scan)
        return path

    @pytest.fixture
    def bent_file(self, scan: NDArray[np.uint8], tmp_path: Path) -> Path:
        path = tmp_path / "bent.png"
        cv2.imwrite(str(path), curl(scan, center=(0.80, 0.83), amplitude_px=60.0))
        return path

    def test_a_clean_sheet_completes_with_a_passing_assessment(
        self, flat_file: Path, template: OmrTemplate
    ) -> None:
        result = recognise_scan(flat_file, template)
        assert result.outcome is RecognitionOutcome.COMPLETE
        assert result.scan_quality is not None
        assert result.scan_quality.status is ScanQualityStatus.PASS
        assert StatusCode.SCAN_QUALITY_REVIEW.value not in result.status_codes

    def test_a_bent_sheet_is_flagged(
        self, bent_file: Path, template: OmrTemplate
    ) -> None:
        result = recognise_scan(bent_file, template)
        assert result.outcome is RecognitionOutcome.REVIEW
        assert StatusCode.SCAN_QUALITY_REVIEW.value in result.status_codes

    def test_a_bent_sheet_keeps_every_answer_it_read(
        self, flat_file: Path, bent_file: Path, template: OmrTemplate
    ) -> None:
        """§6: flag it, do not throw it away.

        Discarding a script whose identity and most of whose answers are
        perfectly legible, because one corner curled, loses more than it
        protects. The values stay; the sheet is merely marked for a human.
        """
        clean = recognise_scan(flat_file, template)
        bent = recognise_scan(bent_file, template)
        assert len(bent.answers) == len(clean.answers)
        assert bent.answers, "recognition must not be abandoned"

    def test_the_assessment_survives_the_project_file(
        self, bent_file: Path, template: OmrTemplate
    ) -> None:
        """Reproducible and explainable after the project is reopened."""
        import json

        from omr_scanner.services.recognition_models import ScanResult

        result = recognise_scan(bent_file, template)
        restored = ScanResult.from_dict(json.loads(json.dumps(result.to_dict())))
        assert restored.scan_quality == result.scan_quality

    def test_reading_the_same_sheet_twice_gives_the_same_verdict(
        self, bent_file: Path, template: OmrTemplate
    ) -> None:
        """Determinism, which the multiprocessing batch depends on."""
        first = recognise_scan(bent_file, template)
        second = recognise_scan(bent_file, template)
        assert first.scan_quality == second.scan_quality

    def test_the_check_can_be_turned_off(
        self, bent_file: Path, template: OmrTemplate
    ) -> None:
        from omr_scanner.services.recognition_settings import RecognitionOptions

        result = recognise_scan(
            bent_file, template, options=RecognitionOptions(check_page_geometry=False)
        )
        assert result.scan_quality is None

    def test_it_reaches_the_review_queue_as_its_own_category(
        self, bent_file: Path, template: OmrTemplate
    ) -> None:
        """Distinguishable from an identity conflict, which is the §7 requirement."""
        result = recognise_scan(bent_file, template)
        conflicts = detect_conflicts(result, template)
        kinds = {item.conflict_type for item in conflicts}
        assert ConflictType.SCAN_QUALITY in kinds
        assert not any(
            item.conflict_type.value.startswith("answer_") for item in conflicts
        )

    def test_a_clean_sheet_raises_no_scan_quality_conflict(
        self, flat_file: Path, template: OmrTemplate
    ) -> None:
        result = recognise_scan(flat_file, template)
        conflicts = detect_conflicts(result, template)
        assert ConflictType.SCAN_QUALITY not in {
            item.conflict_type for item in conflicts
        }
