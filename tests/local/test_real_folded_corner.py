"""Real-world folded-corner validation, run only where the scans exist.

Scope:
    Two real files that live outside the repository, in ``Scratch/Sample
    Scripts/``: a clean reference sheet and the same design scanned with its
    lower-right corner physically folded over.

Why this test is *optional*:
    ``Scratch/`` holds real scans and is never committed - it is ignored under
    both spellings in ``.gitignore`` precisely so a case-insensitive Windows
    checkout and a case-sensitive Linux runner behave the same. So CI has no
    such files, and a suite that failed without them would fail on every runner
    forever. These tests skip cleanly when the files are absent and actually
    execute on a developer machine that has them. The permanently committed
    coverage is in ``tests/unit/test_page_geometry.py``,
    ``tests/unit/test_scan_quality.py`` and
    ``tests/integration/test_scan_quality_sheet.py``, which use generated
    fixtures and the committed sample sheet only.

Why the template is built here rather than committed:
    It is authored from measurements of the reference image through the same
    helpers the template designer uses
    (:mod:`omr_scanner.domain.template_authoring`), so this exercises the real
    authoring path. It stays in this file because it is only meaningful
    alongside the images it describes, and those images are not ours to commit.

What the folded sheet actually does - measured, not assumed:
    The fold covers the bottom-right corner. It destroys the bottom-right
    registration marker outright (production detection finds 3 of 4 markers on
    it, against 4 of 4 on the reference) and occludes the printing for
    questions 92-100. Because a marker is gone, the sheet fails Phase 1
    registration and never reaches the geometry probe at all. That is the
    correct outcome and a stronger rejection than a geometry warning would be -
    but it means the assertion here is "this sheet is not trusted", not "this
    sheet raises a specific geometry code". Asserting the latter would be
    asserting the accident, not the requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.review import ConflictType
from omr_scanner.domain.scan_quality import ScanQualityStatus
from omr_scanner.domain.template import (
    FieldType,
    MarkerRole,
    MarkerShape,
    OmrTemplate,
    OrientationMarker,
    RegistrationMarker,
    SymbolAxis,
)
from omr_scanner.domain.template_authoring import (
    build_blank_template,
    generate_character_grid_zone,
    generate_question_columns,
)
from omr_scanner.services.conflict_policy import detect_conflicts
from omr_scanner.services.marker_detection_service import (
    MarkerSearchConfig,
    detect_registration_markers,
)
from omr_scanner.services.recognition_service import recognise_scan

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPOSITORY_ROOT / "Scratch" / "Sample Scripts"
REFERENCE = SAMPLES / "300-color-contrast-75-br-50.png"
FOLDED = SAMPLES / "corner-mismatch.png"

pytestmark = pytest.mark.skipif(
    not REFERENCE.exists() or not FOLDED.exists(),
    reason="SKIPPED - local real-world fixture unavailable (Scratch/Sample Scripts)",
)

# --- geometry measured from the reference image, in its own pixels ----------
PAGE_W, PAGE_H = 2572, 3311
MARKER_CENTRES_PX = {
    MarkerRole.TOP_LEFT: (138.5, 123.5),
    MarkerRole.TOP_RIGHT: (2384.0, 121.7),
    MarkerRole.BOTTOM_RIGHT: (2384.5, 3049.5),
    MarkerRole.BOTTOM_LEFT: (134.8, 3042.3),
}
MARKER_SIZE_PX = (38.0, 38.0)
ORIENTATION_CENTRE_PX = (195.1, 192.4)
ORIENTATION_SIZE_PX = (34.0, 34.0)
BUBBLE_SIZE_PX = (26.0, 24.0)
ROLL_FIRST_CENTRE_PX = (264.0, 759.0)
ROLL_PITCH_PX = (45.0, 44.9)
ROLL_COLUMNS, ROLL_ROWS = 8, 10
BLOCK_BUBBLE_X_PX = [
    (307.0, 502.0),
    (749.0, 944.0),
    (1189.0, 1384.0),
    (1631.0, 1826.0),
    (2073.0, 2268.0),
]
BLOCK_BUBBLE_Y_PX = (1962.0, 2978.0)
QUESTIONS_PER_BLOCK = 20
ANSWER_LABELS = ("a", "b", "c", "d")


def _rect(x0: float, y0: float, x1: float, y1: float) -> NormalizedRect:
    """Normalise a pixel rectangle onto the reference page."""
    return NormalizedRect(
        x=x0 / PAGE_W, y=y0 / PAGE_H, width=(x1 - x0) / PAGE_W, height=(y1 - y0) / PAGE_H
    )


@pytest.fixture(scope="module")
def baec_template() -> OmrTemplate:
    """The BAEC sheet, authored from measurements of the reference image."""
    bubble = NormalizedSize(width=BUBBLE_SIZE_PX[0] / PAGE_W, height=BUBBLE_SIZE_PX[1] / PAGE_H)
    template = build_blank_template(
        name="BAEC Recruitment (Medical Officer)",
        canonical_width_px=PAGE_W,
        canonical_height_px=PAGE_H,
    )
    markers = tuple(
        RegistrationMarker(
            role=role,
            shape=MarkerShape.FILLED_SQUARE,
            center=NormalizedPoint(x=cx / PAGE_W, y=cy / PAGE_H),
            size=NormalizedSize(
                width=MARKER_SIZE_PX[0] / PAGE_W, height=MARKER_SIZE_PX[1] / PAGE_H
            ),
            search_radius=0.06,
        )
        for role, (cx, cy) in MARKER_CENTRES_PX.items()
    )
    orientation = OrientationMarker(
        shape=MarkerShape.FILLED_SQUARE,
        center=NormalizedPoint(
            x=ORIENTATION_CENTRE_PX[0] / PAGE_W, y=ORIENTATION_CENTRE_PX[1] / PAGE_H
        ),
        size=NormalizedSize(
            width=ORIENTATION_SIZE_PX[0] / PAGE_W, height=ORIENTATION_SIZE_PX[1] / PAGE_H
        ),
        expected_near=MarkerRole.TOP_LEFT,
        search_radius=0.06,
    )
    zones = [
        generate_character_grid_zone(
            zone_id="roll_number",
            label="Roll number",
            field_type=FieldType.NUMERIC,
            symbols=tuple("0123456789"),
            character_count=ROLL_COLUMNS,
            bounds=_rect(
                ROLL_FIRST_CENTRE_PX[0] - BUBBLE_SIZE_PX[0] / 2,
                ROLL_FIRST_CENTRE_PX[1] - BUBBLE_SIZE_PX[1] / 2,
                ROLL_FIRST_CENTRE_PX[0]
                + (ROLL_COLUMNS - 1) * ROLL_PITCH_PX[0]
                + BUBBLE_SIZE_PX[0] / 2,
                ROLL_FIRST_CENTRE_PX[1]
                + (ROLL_ROWS - 1) * ROLL_PITCH_PX[1]
                + BUBBLE_SIZE_PX[1] / 2,
            ),
            bubble_size=bubble,
            symbol_axis=SymbolAxis.VERTICAL,
        )
    ]
    for index, (x0, x1) in enumerate(BLOCK_BUBBLE_X_PX):
        zones.extend(
            generate_question_columns(
                id_prefix=f"questions_{index}",
                label_prefix=f"Questions block {index + 1}",
                first_question=index * QUESTIONS_PER_BLOCK + 1,
                question_count=QUESTIONS_PER_BLOCK,
                answer_labels=ANSWER_LABELS,
                columns=1,
                questions_per_column=QUESTIONS_PER_BLOCK,
                bounds=_rect(x0, BLOCK_BUBBLE_Y_PX[0], x1, BLOCK_BUBBLE_Y_PX[1]),
                bubble_size=bubble,
                symbol_axis=SymbolAxis.HORIZONTAL,
            )
        )
    return template.model_copy(
        update={
            "registration_markers": markers,
            "orientation_marker": orientation,
            "zones": tuple(zones),
        }
    )


class TestTheReferenceSheet:
    """The clean sheet must be measured, and measured as sound."""

    def test_it_registers(self, baec_template: OmrTemplate) -> None:
        result = recognise_scan(REFERENCE, baec_template)
        assert result.registration.value == "registered"

    def test_its_geometry_is_actually_evaluated(
        self, baec_template: OmrTemplate
    ) -> None:
        """The distinction the whole feature turns on.

        ``evaluated=False`` with ``affected=0`` is a measurement failure, not a
        clean sheet, and must never be reported as one.
        """
        assessment = recognise_scan(REFERENCE, baec_template).scan_quality
        assert assessment is not None
        assert assessment.evaluated, "the reference must be measured, not skipped"
        assert assessment.matched_ratio > 0.9, (
            f"probe coverage too thin to conclude anything: "
            f"{assessment.matched_count}/{assessment.probe_count}"
        )

    def test_it_is_confirmed_clean(self, baec_template: OmrTemplate) -> None:
        assessment = recognise_scan(REFERENCE, baec_template).scan_quality
        assert assessment is not None
        assert assessment.status is ScanQualityStatus.PASS
        assert assessment.affected_count == 0
        assert assessment.is_confirmed_clean

    def test_no_scan_quality_conflict_is_raised(
        self, baec_template: OmrTemplate
    ) -> None:
        """No false positive on a geometrically valid page."""
        result = recognise_scan(REFERENCE, baec_template)
        kinds = {item.conflict_type for item in detect_conflicts(result, baec_template)}
        assert ConflictType.SCAN_QUALITY not in kinds

    def test_all_four_markers_are_found(self) -> None:
        outcome = detect_registration_markers(
            REFERENCE,
            config=MarkerSearchConfig(
                expected_marker_width=0.0148,
                expected_marker_height=0.0115,
                corner_search_fraction=0.18,
            ),
        )
        assert sum(1 for item in outcome.markers.values() if item.found) == 4


class TestTheFoldedSheet:
    """The folded sheet must not come out looking trustworthy."""

    def test_the_fold_destroys_one_registration_marker(self) -> None:
        """The measured mechanism, pinned down so a regression is visible.

        This is *why* the sheet is rejected, and it is worth asserting
        separately from the rejection itself: if a future change made the
        detector find a marker inside the fold, the sheet would start
        registering and the reason this test passes would have changed
        completely.
        """
        outcome = detect_registration_markers(
            FOLDED,
            config=MarkerSearchConfig(
                expected_marker_width=0.0148,
                expected_marker_height=0.0115,
                corner_search_fraction=0.18,
            ),
        )
        found = {name for name, item in outcome.markers.items() if item.found}
        assert "bottom_right" not in found
        assert found == {"top_left", "top_right", "bottom_left"}

    def test_it_is_never_reported_as_a_trustworthy_scan(
        self, baec_template: OmrTemplate
    ) -> None:
        """The requirement, stated in the only way that cannot rot.

        Deliberately not "it raises PAGE_GEOMETRY_DISTORTION". On this sheet the
        fold removed a registration marker, so Phase 1 refuses the page before
        the geometry probe runs - a stronger rejection, but a different code. A
        fold that spared the markers would arrive as a geometry finding
        instead. Both are acceptable; being silently trusted is not.
        """
        result = recognise_scan(FOLDED, baec_template)
        assessment = result.scan_quality
        assert assessment is not None, (
            "a sheet must never carry 'no opinion' about its own geometry"
        )
        assert not assessment.is_confirmed_clean
        assert result.outcome.value != "complete"

    def test_the_geometry_state_is_explicit_rather_than_absent(
        self, baec_template: OmrTemplate
    ) -> None:
        """"Not evaluated" has to be recorded, not implied by a missing field."""
        assessment = recognise_scan(FOLDED, baec_template).scan_quality
        assert assessment is not None
        assert not assessment.evaluated
        assert assessment.reason

    def test_it_reaches_the_review_workflow(self, baec_template: OmrTemplate) -> None:
        result = recognise_scan(FOLDED, baec_template)
        conflicts = detect_conflicts(result, baec_template)
        assert conflicts, "the sheet must reach a human"
        assert all(item.conflict_type.requires_resolution for item in conflicts)

    def test_the_two_sheets_are_distinguishable(
        self, baec_template: OmrTemplate
    ) -> None:
        """The comparison that matters: same design, different verdict."""
        clean = recognise_scan(REFERENCE, baec_template).scan_quality
        folded = recognise_scan(FOLDED, baec_template).scan_quality
        assert clean is not None and folded is not None
        assert clean.is_confirmed_clean
        assert not folded.is_confirmed_clean
