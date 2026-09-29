"""Rendering a planned dataset: images, ground truth and a manifest.

Purpose:
    Take the case plan
    (:mod:`omr_scanner.evaluation.case_plans`) and turn it into files on disk:
    a scan-like image per sheet at a realistic resolution, a ground-truth
    document beside it, and a manifest that makes the whole thing
    reproducible.

Responsibilities:
    * :func:`sheet_spec_from_template` - project a real ``.omrt`` template onto
      a renderable page.
    * :func:`render_case` - one planned case to one image plus its truth.
    * :func:`generate_dataset` - the whole dataset, with progress and
      cancellation, including one answer key and solution sheet per set
      (:mod:`omr_scanner.evaluation.answer_keys`) and candidate answers decided
      against those keys (:mod:`omr_scanner.evaluation.performance`).

What does NOT belong here:
    * Deciding *what* to test - that is `case_plans`, and keeping the two
      apart is what stops the renderer from quietly drawing whatever the
      engine happens to read.
    * Recognition, or any judgement about it
      (:mod:`omr_scanner.evaluation.benchmark`).

Why the template drives everything:
    Page size, markers, orientation mark, zone geometry and bubble grids all
    come from the selected template, so a dataset exercises the coordinate
    mapping the engine really uses:

    ```text
        one template definition
                 │
          ┌──────┴──────┐
          ▼             ▼
    real scanning   synthetic generation
          └──────┬──────┘
                 ▼
        the same recognition geometry
    ```

    A generator with its own idea of where bubbles go would test the two
    halves of the application against each other's mistakes.

Two rendering modes:
    :class:`RenderMode` decides where the *page* comes from. The marks, the
    answers and every piece of ground truth are decided identically either way,
    before anything is drawn, which is the property that makes the two modes'
    results comparable:

    ```text
        case plan  (answers, identifier, set code, degradation)
              │
      ┌───────┴────────┐
      ▼                ▼
    TEMPLATE      REFERENCE_SCAN
    draw the      draw only the marks and
    whole page    lay them on a real blank scan
      └───────┬────────┘
              ▼
        the same ground truth
    ```

Resolution:
    In :attr:`RenderMode.TEMPLATE`, sheets are rendered at :data:`DEFAULT_DPI`
    from the template's *physical* page size in millimetres, not at its
    canonical pixel size. That is the honest thing to do - a real scan is
    whatever the scanner produced, and rendering at the canonical size would
    hand the engine a page that needed no rescaling and quietly stop testing
    one.

    In :attr:`RenderMode.REFERENCE_SCAN` the pixels already exist, so the DPI
    setting has nothing to act on and is not applied; the output is the
    reference scan at its native resolution. See
    :mod:`omr_scanner.evaluation.reference_scan`.

An honest warning, stated here because it is easy to forget:
    Synthetic accuracy is not real accuracy. In :attr:`RenderMode.TEMPLATE`
    these pages have clean geometry, even paper and marks drawn by arithmetic.
    They measure *regression consistency and controlled edge-case handling*,
    and are nearly useless as evidence that a threshold is right for real
    pencil on real paper.

    :attr:`RenderMode.REFERENCE_SCAN` narrows that gap - the paper, the print,
    the illumination and the scanner's own behaviour are real - but it does not
    close it. The *marks* are still drawn by arithmetic, and a real candidate's
    pencil is not an ellipse. It is better evidence, not sufficient evidence.
"""

from __future__ import annotations

import csv
import json
import logging
import random
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.template import IgnoredFieldDefinition
from omr_scanner.evaluation.answer_keys import (
    SOLUTION_DIRNAME,
    SOLUTION_ROLE,
    SolutionFiles,
    SyntheticAnswerKey,
    clear_stale_solutions,
    describe_solutions,
    generate_answer_keys,
    planned_set_codes,
    serialise_answer_key,
    set_code_markable,
    solution_case,
    solution_file_names,
)
from omr_scanner.evaluation.attendance_dataset import (
    ATTENDANCE_DIRNAME,
    Population,
    bind_case,
    write_ground_truth,
    write_workbooks,
)
from omr_scanner.evaluation.attendance_dataset import (
    summarise as summarise_population,
)
from omr_scanner.evaluation.case_plans import (
    CORNER_ROLES,
    DatasetProfile,
    families_for,
    plan_dataset,
)
from omr_scanner.evaluation.fold_plans import (
    NO_FOLDS,
    FoldCorner,
    FoldPolicy,
    FoldSeverity,
    MarkerOutline,
    describe_folds,
    marker_outlines,
    plan_folds,
)
from omr_scanner.evaluation.ground_truth import (
    DatasetManifest,
    SheetGroundTruth,
    save_ground_truth,
    save_manifest,
)
from omr_scanner.evaluation.performance import (
    DEFAULT_PERFORMANCE,
    PerformancePolicy,
    apply_performance,
    summarise_scores,
)
from omr_scanner.evaluation.reference_scan import (
    ReferenceScan,
    load_reference_scan,
    render_onto_reference,
)
from omr_scanner.evaluation.test_cases import (
    CaseFamily,
    FieldLayout,
    MarkPlan,
    SheetCase,
    TestCaseTag,
)
from omr_scanner.imaging.folds import PagePlacement, apply_corner_folds
from omr_scanner.imaging.synthetic import (
    AnswerBubbleSpec,
    ColorMode,
    DistortionSpec,
    MarkStyle,
    SyntheticSheetSpec,
    capture_channels,
    distort_image,
    quantise_to_output,
    render_mark_layer,
    render_sheet,
)
from omr_scanner.recognition.fields import zone_groups
from omr_scanner.services.recognition_models import utc_timestamp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.attendance_dataset import SyntheticCandidate

_LOGGER = logging.getLogger(__name__)

GENERATOR_VERSION = "2.3"
"""Version of this generator, recorded in every manifest.

Changing how a defect is drawn changes what a dataset means, so a benchmark
result that does not say which generator produced its data is not comparable
with anything. Bumped to 2.0 when named test cases, DPI-based rendering and
JPEG output arrived, to 2.1 when the reference-scan rendering mode and the
colour modes did, to 2.2 for physical corner folds, and to 2.3 when answer
keys, solution sheets and key-relative candidate performance arrived."""

DATASET_SCHEMA_VERSION = "1.0"
"""Version of the dataset *layout* - the directories and the manifest."""

IMAGES_DIRNAME = "images"
GROUND_TRUTH_DIRNAME = "ground_truth"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_CSV_FILENAME = "manifest.csv"
SUMMARY_FILENAME = "dataset_summary.json"

DEFAULT_PREFIX = "SYN"
"""Prefix for generated file names: ``SYN_000001.png``. Deliberately not
anything that could be mistaken for a real candidate's identifier."""

DEFAULT_DPI = 150
"""Rendering resolution, in dots per inch.

The resolution an office scanner is usually set to for forms, and high enough
that a bubble is tens of pixels across at any sensible sheet design."""

MM_PER_INCH = 25.4

DEFAULT_JPEG_QUALITY = 92
"""Quality for JPEG output: visually lossless, and still a JPEG.

High on purpose. Compression *damage* is a degradation case with its own tag
and its own parameter; it should not arrive uninvited in every dataset that
happens to be written as JPEG."""


class RenderMode(StrEnum):
    """Where a generated sheet's *page* comes from.

    Only the page. What is marked on it, what the correct answers are and what
    degradation is applied are decided by the case plan before either mode sees
    anything, so a dataset generated twice - once each way - differs in its
    pixels and in nothing else.
    """

    TEMPLATE = "template"
    """Draw the whole page from the template: markers, orientation mark, bubble
    rings, printed option letters and the marks. The default, and what this
    generator has always done, so an existing call is unaffected."""

    REFERENCE_SCAN = "reference_scan"
    """Draw only the candidate's marks and lay them on a scan of a real blank
    form. The paper, the print, the illumination and the scanner's own
    behaviour are then real rather than modelled; see
    :mod:`omr_scanner.evaluation.reference_scan`."""


RENDER_FROM_REFERENCE_SCAN = "reference_scan"
"""``PageRender.derived_from`` for a page that came from a real scan.

Alongside the existing ``"physical"`` and ``"canonical"``. A reader of a
manifest can therefore tell all three apart without knowing which mode was
used."""

UNDECLARED_DPI = 0
"""``PageRender.dpi`` when the resolution is genuinely not known.

A real scan does not record what it was scanned at in any form this generator
can read, and inventing the requested DPI would put a number in the manifest
that describes nothing. Written out as ``null``, not ``0``, so that a reader
sees "not applicable" rather than a resolution of zero."""


MARKER_DEFECT_TAGS: frozenset[TestCaseTag] = frozenset(
    {
        TestCaseTag.MARKER_FAINT,
        TestCaseTag.MARKER_DAMAGED,
        TestCaseTag.MARKER_MISSING,
        TestCaseTag.MARKERS_MISSING_MANY,
        TestCaseTag.MARKER_EXTRA,
        TestCaseTag.ORIENTATION_MISSING,
        TestCaseTag.ORIENTATION_FAINT,
    }
)
"""Tags that describe damage to printed registration or orientation marks.

These are the cases :attr:`RenderMode.REFERENCE_SCAN` cannot produce: the
markers on a reference sheet were printed and photographed long before this
generator ran, and it draws only the candidate's ink. Damaging them would mean
painting over a real scan, which is Checkpoint B's marker-aware occlusion work
and not something to fake in the meantime."""

INDEPENDENT_FAILURE_TAGS: frozenset[TestCaseTag] = frozenset(
    {TestCaseTag.CROP_SEVERE}
)
"""Tags that predict a refused registration for a reason unrelated to marker
damage.

Used for exactly one decision: when :func:`strip_unrenderable_defects` removes
a case's marker damage, does its ``expect_failure`` survive? It survives only
if the sheet would still fail for one of these reasons. A sheet that was
expected to fail *because a marker was missing*, and then had the missing
marker put back, is an ordinary sheet and must be scored as one."""


class ImageFormat(StrEnum):
    """The formats a dataset can be written in."""

    PNG = "png"
    """Lossless. The right default for a regression baseline: two runs of the
    same seed produce byte-identical files."""

    JPEG = "jpg"
    """What most scanners actually emit, and therefore worth testing against
    even at high quality."""

    @property
    def suffix(self) -> str:
        """The file extension, with its dot."""
        return f".{self.value}"

    @classmethod
    def parse(cls, value: str) -> ImageFormat:
        """Accept ``png``, ``jpg`` or ``jpeg``, however it was capitalised."""
        text = value.strip().lower().lstrip(".")
        if text in {"jpg", "jpeg"}:
            return cls.JPEG
        if text == "png":
            return cls.PNG
        raise ValueError(f"Unsupported image format '{value}'; use png or jpg")


@dataclass(frozen=True, slots=True)
class PageRender:
    """The pixel size one sheet is rendered at, and where it came from.

    Attributes:
        width: Image width in pixels.
        height: Image height in pixels.
        dpi: The resolution used.
        derived_from: ``"physical"`` when the template declared millimetres,
            ``"canonical"`` when the pixel size had to be used instead.
    """

    width: int
    height: int
    dpi: int
    derived_from: str


def page_render_size(template: OmrTemplate, dpi: int = DEFAULT_DPI) -> PageRender:
    """Return the pixel size a sheet should be rendered at.

    Derived from the template's *physical* page size:
    ``mm / 25.4 * dpi``. A4 at 150 dpi is 1240 x 1754 px, Letter 1275 x 1650.

    Falls back to the template's canonical pixel size only when the physical
    dimensions are missing or nonsensical - never to an invented page size,
    because a dataset rendered at the wrong aspect ratio would fail
    registration for a reason that has nothing to do with recognition.

    Raises:
        ValueError: ``dpi`` is not positive.
    """
    if dpi <= 0:
        raise ValueError(f"DPI must be positive, got {dpi}")

    page = template.page
    width_mm = float(getattr(page, "width_mm", 0.0) or 0.0)
    height_mm = float(getattr(page, "height_mm", 0.0) or 0.0)
    if width_mm > 0.0 and height_mm > 0.0:
        return PageRender(
            width=max(round(width_mm / MM_PER_INCH * dpi), 1),
            height=max(round(height_mm / MM_PER_INCH * dpi), 1),
            dpi=dpi,
            derived_from="physical",
        )

    return PageRender(
        width=page.canonical_width_px,
        height=page.canonical_height_px,
        dpi=dpi,
        derived_from="canonical",
    )


def sheet_spec_from_template(
    template: OmrTemplate,
    marks: Mapping[str, Mapping[int, MarkPlan]] | None = None,
    *,
    base: SyntheticSheetSpec | None = None,
    omit_markers: Sequence[str] = (),
    faint_markers: Sequence[str] = (),
    damaged_markers: Sequence[str] = (),
    extra_marker: bool = False,
    omit_orientation: bool = False,
    faint_orientation: bool = False,
    render: PageRender | None = None,
) -> SyntheticSheetSpec:
    """Return a renderable page carrying every bubble ``template`` declares.

    Args:
        template: The template whose geometry the page should match.
        marks: ``{zone id: {group key: plan}}``. A group left out is drawn
            empty. The group key is the character position for a grid field and
            the question offset within its block for a question block - the
            same key :func:`~omr_scanner.recognition.fields.zone_groups` uses,
            so the generator and the recogniser cannot disagree about which
            bubble is which.
        base: Starting page specification, for callers that want the default
            decoy graphics.
        omit_markers: Registration marker roles to leave off the page.
        faint_markers: Roles printed pale rather than black.
        damaged_markers: Roles with a corner torn away.
        extra_marker: Draw one additional marker-shaped decoy, to test that
            selection uses position and not merely "the four blackest shapes".
        omit_orientation: Leave the orientation mark off.
        faint_orientation: Print the orientation mark pale.
        render: Pixel size to render at; the template's canonical size when
            omitted.

    Returns:
        A specification whose page size, markers, orientation mark and bubbles
        all come from the template.
    """
    from omr_scanner.domain.geometry import NormalizedPoint
    from omr_scanner.domain.template import MarkerRole

    chosen = marks or {}
    start = base if base is not None else SyntheticSheetSpec()
    bubbles: list[AnswerBubbleSpec] = []

    for zone in template.zones:
        grid = zone.grid
        if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        zone_marks = chosen.get(zone.id, {})
        for group in zone_groups(zone):
            plan = zone_marks.get(group.key)
            marked = set(plan.labels) if plan is not None else set()
            for index, cell in enumerate(group.cells):
                center = grid.bubble_center(*cell)
                label = group.labels[index]
                is_marked = label in marked
                bubbles.append(
                    AnswerBubbleSpec(
                        center=center,
                        width=grid.bubble_size.width,
                        height=grid.bubble_size.height,
                        fill=plan.fill if (is_marked and plan is not None) else 0.0,
                        # The printed symbol is why an *empty* bubble contains
                        # ink; a mark covers it, so it is only drawn when the
                        # bubble is empty and the symbol is a single character.
                        symbol=label if len(label) == 1 else "",
                        style=plan.style if plan is not None else MarkStyle.FILL,
                        intensity=plan.intensity if plan is not None else 1.0,
                        offset_x=plan.offset_x if plan is not None else 0.0,
                        offset_y=plan.offset_y if plan is not None else 0.0,
                        size_scale=plan.size_scale if plan is not None else 1.0,
                    )
                )

    size = render if render is not None else PageRender(
        width=template.page.canonical_width_px,
        height=template.page.canonical_height_px,
        dpi=DEFAULT_DPI,
        derived_from="canonical",
    )

    decoys = tuple(start.decoy_markers)
    if extra_marker:
        # Inside the page rather than in a corner search region: a false
        # marker that competes without being where a real one belongs.
        decoys = (*decoys, NormalizedPoint(x=0.5, y=0.08))

    return SyntheticSheetSpec(
        width=size.width,
        height=size.height,
        marker_targets={
            marker.role: marker.center for marker in template.registration_markers
        },
        marker_width=_marker_extent(template, axis="width"),
        marker_height=_marker_extent(template, axis="height"),
        orientation_center=template.orientation_marker.center,
        orientation_width=template.orientation_marker.size.width,
        orientation_height=template.orientation_marker.size.height,
        control_points=(),
        draw_bubbles=False,
        # The default page's decoy text bars and answer frames are placed for
        # the *marker detection* tests and sit across the answer area, which
        # would corrupt the very measurements a recognition dataset exists to
        # produce. The symbol printed inside every empty bubble supplies the
        # realistic "there is ink here already" challenge instead.
        draw_text_bars=False,
        draw_answer_frames=False,
        decoy_markers=decoys,
        omit_markers=frozenset(MarkerRole(role) for role in omit_markers),
        faint_markers=frozenset(MarkerRole(role) for role in faint_markers),
        damaged_markers=frozenset(MarkerRole(role) for role in damaged_markers),
        faint_orientation_marker=faint_orientation,
        omit_orientation_marker=omit_orientation,
        answer_bubbles=tuple(bubbles),
    )


def _marker_extent(template: OmrTemplate, *, axis: str) -> float:
    """Return the registration markers' size along one axis.

    Averaged over the four, because a template may declare them individually
    and the renderer draws one size; a hand-edited template with mismatched
    markers should still produce a usable page rather than an exception.
    """
    sizes = [
        marker.size.width if axis == "width" else marker.size.height
        for marker in template.registration_markers
    ]
    return sum(sizes) / len(sizes) if sizes else 0.03


@dataclass(frozen=True, slots=True)
class GeneratedSheet:
    """One rendered sheet and the truth about it.

    Attributes:
        image: The rendered, degraded page.
        truth: What it should read.
    """

    image: NDArray[np.uint8]
    truth: SheetGroundTruth


def strip_unrenderable_defects(case: SheetCase) -> SheetCase:
    """Return ``case`` with every defect a reference scan cannot express removed.

    A reference sheet's registration and orientation marks were printed and
    photographed before this generator existed, and
    :attr:`RenderMode.REFERENCE_SCAN` draws only the candidate's ink. So a case
    asking for a faint, damaged, missing, extra or absent mark cannot be
    honoured in that mode.

    The choice made here is to remove the *claim* along with the defect, rather
    than to drop the sheet or to render it and let the ground truth lie. Three
    things follow, and all three are deliberate:

    * The tag goes too, so nothing in the dataset says ``MARKER_MISSING`` about
      a sheet whose markers are all present. A benchmark that grouped by that
      tag would otherwise report a category it never actually tested.
    * ``expect_failure`` goes, unless the sheet still carries a reason to fail
      that has nothing to do with markers (:data:`INDEPENDENT_FAILURE_TAGS`).
      Putting a missing marker back makes the sheet readable, and a dataset
      that still expected it to be refused would score a correct reading as an
      error.
    * A note is appended saying what was dropped, so a human reading the ground
      truth of a single sheet can see it happened without going back to the
      manifest.

    Everything else - the marks, the answers, the identifier, the set code, the
    geometric and photometric degradation - is untouched, because all of it
    survives the change of rendering mode intact. That is what keeps the two
    modes' ground truth identical for every sheet that carries no marker
    damage, which is the great majority of them.

    Args:
        case: The planned case.

    Returns:
        ``case`` itself when it asks for nothing unrenderable, so the common
        path allocates nothing.
    """
    has_defect = bool(
        case.omit_markers
        or case.faint_markers
        or case.damaged_markers
        or case.extra_marker
        or case.omit_orientation
        or case.faint_orientation
    )
    if not has_defect:
        return case

    remaining = tuple(tag for tag in case.tags if tag not in MARKER_DEFECT_TAGS)
    survives = bool(set(remaining) & INDEPENDENT_FAILURE_TAGS)
    if not survives:
        remaining = tuple(
            tag for tag in remaining if tag is not TestCaseTag.EXPECTED_FAILURE
        )
    if not remaining:
        # Everything it claimed has now gone - the marker-missing case, for
        # instance, was tagged only ``MARKER_MISSING`` and ``EXPECTED_FAILURE``.
        # It is still a real sheet, a correctly filled one, and saying so is
        # necessary: a benchmark groups by tag, and an untagged sheet would be
        # silently dropped from every category it reports. Checked after the
        # expected-failure removal, not before, or that removal could empty a
        # tuple this has already decided was fine.
        remaining = (TestCaseTag.BASELINE,)

    note = (
        "Marker and orientation defects were not applied: this sheet was "
        "rendered onto a real reference scan, whose printed marks the "
        "generator does not alter."
    )
    return replace(
        case,
        tags=remaining,
        omit_markers=(),
        faint_markers=(),
        damaged_markers=(),
        extra_marker=False,
        omit_orientation=False,
        faint_orientation=False,
        expect_failure=case.expect_failure and survives,
        notes=f"{case.notes} {note}".strip(),
    )


def render_case(
    template: OmrTemplate,
    case: SheetCase,
    *,
    render: PageRender | None = None,
    image_format: ImageFormat = ImageFormat.PNG,
    prefix: str = DEFAULT_PREFIX,
    dataset_version: str = "1",
    seed: int = 0,
    render_mode: RenderMode = RenderMode.TEMPLATE,
    reference: ReferenceScan | None = None,
    color_mode: ColorMode = ColorMode.GRAYSCALE,
    outlines: Sequence[MarkerOutline] | None = None,
) -> GeneratedSheet:
    """Render one planned case and return it with its ground truth.

    Args:
        template: The template to draw from.
        case: What this sheet is, from
            :func:`~omr_scanner.evaluation.case_plans.plan_dataset`.
        render: Pixel size; derived from the template at
            :data:`DEFAULT_DPI` when omitted. Ignored in
            :attr:`RenderMode.REFERENCE_SCAN`, where the page's size is the
            reference scan's own.
        image_format: Decides the file name recorded in the ground truth.
        prefix: File-name prefix.
        dataset_version: Recorded in the ground truth.
        seed: The dataset's master seed, recorded so one sheet can be
            reproduced on its own.
        render_mode: Where the page comes from; see :class:`RenderMode`.
        reference: The registered blank scan, required by
            :attr:`RenderMode.REFERENCE_SCAN` and ignored otherwise. Loaded
            once per run by :func:`generate_dataset` rather than per sheet,
            because registering it is the expensive part and its answer does
            not change between sheets.
        color_mode: Channel layout and tonal range of the written image.
        outlines: The template's markers in canonical page coordinates, for
            describing what a fold covered. Computed once per run by
            :func:`generate_dataset` and passed in, because re-reading the
            template for every sheet of a hundred thousand would be the kind of
            per-sheet reparsing this generator exists without. Derived from
            ``template`` when omitted, so a single-sheet caller need not supply
            it.

    Returns:
        The image and the ground truth, which is copied from the case rather
        than re-derived - the case already holds the single statement of what
        was drawn. In :attr:`RenderMode.REFERENCE_SCAN` the case is first put
        through :func:`strip_unrenderable_defects`, so the truth describes what
        was actually produced rather than what was planned.

    Raises:
        ValueError: ``render_mode`` is :attr:`RenderMode.REFERENCE_SCAN` and no
            ``reference`` was supplied.
    """
    if render_mode is RenderMode.REFERENCE_SCAN and reference is None:
        raise ValueError(
            "Rendering onto a reference scan needs one; pass reference=..."
        )

    name = f"{prefix}_{case.index:06d}{image_format.suffix}"
    # A reference passed in template mode is not the page and must not be
    # recorded as though it were; the guard above has already ensured one
    # exists whenever the reference mode actually needs it.
    source = reference if render_mode is RenderMode.REFERENCE_SCAN else None
    effective = case if source is None else strip_unrenderable_defects(case)

    if source is not None:
        size = reference_page_render(source)
        image = _render_onto_scan(template, effective, source, color_mode)
    else:
        size = render if render is not None else page_render_size(template)
        image = _render_from_template(template, effective, size, color_mode)

    metadata: dict[str, Any] = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "case_index": effective.index,
        "render_mode": render_mode.value,
        "color_mode": color_mode.value,
        "reference_scan": source.name if source is not None else "",
        "render": {
            "width": size.width,
            "height": size.height,
            "dpi": size.dpi if size.dpi > 0 else None,
            "derived_from": size.derived_from,
        },
        "mark_styles": _mark_styles(effective),
    }
    if effective.intended is not None:
        # The candidate's intended answers against their own set's key, before
        # any test condition was laid over them. Present only when the
        # performance model decided them, so a legacy-random dataset carries
        # exactly the metadata it always did.
        metadata["performance"] = effective.intended.describe()
    if effective.folds:
        # Present only when there is something to say. A sheet that was not
        # folded carries no fold record at all, so a dataset generated with
        # folding off has exactly the metadata it had before folding existed.
        page_width, page_height = _canonical_page(template)
        metadata["physical_augmentation"] = describe_folds(
            effective.folds,
            outlines if outlines is not None else marker_outlines(
                template, width=page_width, height=page_height
            ),
            width=page_width,
            height=page_height,
        )

    truth = SheetGroundTruth(
        scan=name,
        roll=effective.roll,
        set_code=effective.set_code,
        answers=dict(effective.answers),
        ambiguous=effective.ambiguous_questions,
        expect_failure=effective.expect_failure,
        tags=effective.tag_values,
        roll_marks=effective.roll_marks,
        roll_ambiguous=effective.roll_ambiguous,
        set_marks=effective.set_marks,
        set_ambiguous=effective.set_ambiguous,
        duplicate_group=effective.duplicate_group,
        degradation=_degradation_summary(effective),
        notes=effective.notes,
        dataset_version=dataset_version,
        metadata=metadata,
    )
    return GeneratedSheet(image=image, truth=truth)


def _canonical_page(template: OmrTemplate) -> tuple[float, float]:
    """The page size fold geometry and marker overlap are expressed in.

    The template's own canonical size, in both rendering modes and at every
    resolution. That is what makes a fold's recorded depth mean the same thing
    on a 150 dpi render and on a 600 dpi scan, and what makes the two modes'
    overlap fractions comparable at all: an overlap measured in scan pixels
    would depend on how the sheet happened to lie on the platen, because a
    homography does not preserve area ratios.
    """
    return (
        float(template.page.canonical_width_px),
        float(template.page.canonical_height_px),
    )


def reference_page_render(reference: ReferenceScan) -> PageRender:
    """Describe a reference scan the way a rendered page is described.

    So that one ``render`` block in the ground truth means the same thing in
    both modes: the size of the page *before* degradation, and where that size
    came from. The DPI is :data:`UNDECLARED_DPI`, because a scan does not carry
    one that this generator can read - see that constant.
    """
    return PageRender(
        width=reference.width,
        height=reference.height,
        dpi=UNDECLARED_DPI,
        derived_from=RENDER_FROM_REFERENCE_SCAN,
    )


def _render_from_template(
    template: OmrTemplate,
    case: SheetCase,
    size: PageRender,
    color_mode: ColorMode,
) -> NDArray[np.uint8]:
    """Draw the whole page, then degrade it: the original rendering mode."""
    spec = sheet_spec_from_template(
        template,
        case.marks,
        omit_markers=case.omit_markers,
        faint_markers=case.faint_markers,
        damaged_markers=case.damaged_markers,
        extra_marker=case.extra_marker,
        omit_orientation=case.omit_orientation,
        faint_orientation=case.faint_orientation,
        render=size,
    )
    sheet = render_sheet(spec)
    # Colour before degradation, bilevel after it - see `ColorMode`. With the
    # grayscale default both calls are no-ops and the pixels are exactly what
    # this function has always produced.
    captured = capture_channels(sheet.image, color_mode)
    folded = _apply_folds(captured, case, template)
    return quantise_to_output(distort_image(folded, case.distortion), color_mode)


def _render_onto_scan(
    template: OmrTemplate,
    case: SheetCase,
    reference: ReferenceScan,
    color_mode: ColorMode,
) -> NDArray[np.uint8]:
    """Draw only the marks, lay them on the real scan, then degrade the result.

    The mark layer is rendered at the size the reference asks for rather than
    at the DPI-derived page size, because it is about to be warped into that
    scan's pixels and rendering it smaller would cost sharpness that cannot be
    recovered afterwards.
    """
    layer_width, layer_height = reference.mark_layer_size
    spec = sheet_spec_from_template(
        template,
        case.marks,
        render=PageRender(
            width=layer_width,
            height=layer_height,
            dpi=UNDECLARED_DPI,
            derived_from=RENDER_FROM_REFERENCE_SCAN,
        ),
    )
    composited = render_onto_reference(reference, render_mark_layer(spec))
    folded = _apply_folds(composited, case, template, reference=reference)
    return quantise_to_output(distort_image(folded, case.distortion), color_mode)


def _apply_folds(
    image: NDArray[np.uint8],
    case: SheetCase,
    template: OmrTemplate,
    *,
    reference: ReferenceScan | None = None,
) -> NDArray[np.uint8]:
    """Fold the composed sheet, if this case has any folds.

    Called at the same point in both rendering modes, and that point is the
    whole of the physical argument: **after** the page, its printing and the
    candidate's marks have been put together, and **before** the scanner gets
    hold of it. A fold applied earlier would fold the paper without folding
    what is written on it; applied later it would be a fold that the scanner's
    own blur and noise had somehow got underneath.

    The two modes differ only in where the page sits in the image:

    * template-rendered - the page fills the image, described in the
      template's canonical units so that the same fold means the same thing at
      any resolution;
    * reference scan - the page is somewhere inside a larger scan, at whatever
      angle and scale it was fed at, and the registration homography Checkpoint
      A already computed says where. No second registration, and no single
      scalar standing in for two different axis scales.
    """
    if not case.folds:
        return image
    page_width, page_height = _canonical_page(template)
    placement = (
        PagePlacement(
            width=float(reference.canonical_width),
            height=float(reference.canonical_height),
            to_image=reference.canonical_to_scan,
        )
        if reference is not None
        else PagePlacement.scaled_to(image, width=page_width, height=page_height)
    )
    return apply_corner_folds(image, case.folds, placement)


def _degradation_summary(case: SheetCase) -> dict[str, Any]:
    """Return everything that was done to this sheet, and nothing that was not.

    Only the non-default parameters: a record listing twenty defaults tells a
    reader nothing about which sheet they are looking at, and a reader trying
    to reproduce a failure wants the three numbers that mattered.
    """
    default = DistortionSpec()
    summary: dict[str, Any] = {}
    for name in (
        "rotation_degrees", "scale_x", "scale_y", "translate_x_px", "translate_y_px",
        "perspective_strength", "margin_px", "brightness_gain", "brightness_offset",
        "illumination_gradient", "blur_kernel_px", "noise_sigma", "jpeg_quality",
        "paper_gray", "speckle_density", "streak_strength", "edge_shadow",
    ):
        value = getattr(case.distortion, name)
        if value != getattr(default, name):
            summary[name] = value

    if case.omit_markers:
        summary["omitted_markers"] = list(case.omit_markers)
    if case.faint_markers:
        summary["faint_markers"] = list(case.faint_markers)
    if case.damaged_markers:
        summary["damaged_markers"] = list(case.damaged_markers)
    if case.extra_marker:
        summary["extra_marker"] = True
    if case.omit_orientation:
        summary["omitted_orientation_mark"] = True
    if case.faint_orientation:
        summary["faint_orientation_mark"] = True
    return summary


def _mark_styles(case: SheetCase) -> dict[str, str]:
    """Return the style drawn for each answered question.

    Recorded per question because when a benchmark reports fifty false blanks,
    the first thing worth knowing is whether they were all ticks.
    """
    styles: dict[str, str] = {}
    for zone_plans in case.marks.values():
        for plan in zone_plans.values():
            if plan.labels and plan.style is not MarkStyle.FILL:
                styles.setdefault(plan.style.value, plan.style.value)
    return styles


# ----------------------------------------------------------------------
# Writing a whole dataset
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenerationProgress:
    """One progress report while a dataset is being written.

    Attributes:
        completed: Sheets written so far.
        total: Sheets the dataset will contain.
        name: The file just written.
    """

    completed: int
    total: int
    name: str


def generate_dataset(
    output_dir: Path,
    template: OmrTemplate,
    *,
    count: int = 24,
    seed: int = 20260918,
    profile: DatasetProfile = DatasetProfile.MIXED,
    custom_families: Sequence[CaseFamily] | None = None,
    dpi: int = DEFAULT_DPI,
    image_format: ImageFormat | str = ImageFormat.PNG,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    name: str = "synthetic",
    version: str = "1",
    prefix: str = DEFAULT_PREFIX,
    template_path: str = "",
    write_metadata: bool = True,
    population: Population | None = None,
    render_mode: RenderMode = RenderMode.TEMPLATE,
    reference_scan: Path | None = None,
    color_mode: ColorMode = ColorMode.GRAYSCALE,
    fold_policy: FoldPolicy | None = None,
    generate_solutions: bool = True,
    performance: PerformancePolicy | None = None,
    on_progress: Callable[[GenerationProgress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> DatasetManifest:
    """Write a complete labelled dataset to disk.

    Args:
        output_dir: Folder to create ``images/``, ``ground_truth/`` and the
            manifests in.
        template: The template to render from.
        count: How many sheets.
        seed: Master seed. The same seed, count, profile, template and format
            produce the same dataset, which is what makes a benchmark
            comparable between two runs and two engine versions.
        profile: Which families of test case to draw on.
        custom_families: Families to use when ``profile`` is ``CUSTOM``.
        dpi: Rendering resolution.
        image_format: ``png`` or ``jpg``.
        jpeg_quality: Quality for JPEG output.
        name: Dataset name, recorded in the manifest.
        version: Dataset revision.
        prefix: File-name prefix.
        template_path: Where the template came from, recorded in the manifest.
        write_metadata: Also write ``manifest.csv`` and
            ``dataset_summary.json``. The per-sheet ground truth is always
            written - without it the dataset is just pictures.
        population: An attendance population from
            :func:`~omr_scanner.evaluation.attendance_dataset.plan_population`.
            When given, it decides *identity*: how many sheets exist, whose
            script each one is, and what identifier and set code it carries -
            including the deliberately blank, duplicated and unknown ones. The
            case plan still decides everything about the image. The run also
            writes ``attendance/`` and the two reconciliation ground-truth
            files. ``count`` is ignored, because the population already
            answered it.
        render_mode: Where each sheet's page comes from; see
            :class:`RenderMode`.
        reference_scan: The blank scan to lay marks on. Required by
            :attr:`RenderMode.REFERENCE_SCAN` and ignored otherwise. Loaded and
            registered **once**, before any sheet is rendered, so a file that
            cannot be used fails the run immediately rather than after several
            thousand images have been written.
        color_mode: Channel layout and tonal range of every written image.
        fold_policy: Physical corner folds; see
            :class:`~omr_scanner.evaluation.fold_plans.FoldPolicy`. Disabled
            when omitted, and a disabled policy leaves every sheet exactly as
            it would have been - which is what keeps every existing caller and
            every existing dataset unaffected.
        generate_solutions: Also write ``solution/``: one clean solution OMR
            sheet, one answer-key text file and one ground-truth document per
            set. On by default. The keys are drawn either way - candidates'
            answers are decided against them - so turning this off changes
            which files are written and nothing about any candidate's sheet.
        performance: How each candidate's share of correct answers is decided;
            see :mod:`omr_scanner.evaluation.performance`. A truncated normal
            distribution (mean 65 %, SD 15 %) when omitted;
            :data:`~omr_scanner.evaluation.performance.LEGACY_RANDOM` restores
            answers chosen independently of any key.
        on_progress: Called after each sheet is written. Runs on the calling
            thread, so a GUI caller must marshal to the main thread.
        should_cancel: Polled before each sheet; returning ``True`` stops the
            run and writes a manifest describing what was actually produced.

    Returns:
        The manifest, already written.

    Raises:
        ValueError: ``count`` is not positive, the format is unsupported, the
            template declares no bubble grids to fill in,
            :attr:`RenderMode.REFERENCE_SCAN` was asked for without a scan, the
            performance settings are impossible, the template's questions are
            malformed, or a set code cannot be marked on its set-code field.
        ReferenceScanError: The reference scan could not be decoded or could
            not be registered against ``template``.

    Memory:
        One sheet is rendered, encoded, written and released before the next
        begins. A dataset of ten thousand sheets therefore costs one page of
        memory, not ten thousand. In :attr:`RenderMode.REFERENCE_SCAN` one
        additional copy of the reference scan and one mark layer are held for
        the whole run, which is two pages rather than ten thousand and one.
    """
    import cv2

    chosen_format = (
        image_format
        if isinstance(image_format, ImageFormat)
        else ImageFormat.parse(str(image_format))
    )
    if count < 1:
        raise ValueError(f"A dataset needs at least one sheet, got {count}")
    if not any(zone.grid is not None for zone in template.zones):
        raise ValueError(
            "This template declares no bubble grids, so there is nothing to generate"
        )
    if render_mode is RenderMode.REFERENCE_SCAN and reference_scan is None:
        raise ValueError(
            "Rendering onto a real scanned sheet needs a reference scan; "
            "pass reference_scan=..."
        )
    policy = performance if performance is not None else DEFAULT_PERFORMANCE
    policy.validate()

    reference = (
        load_reference_scan(reference_scan, template, color_mode=color_mode)
        if render_mode is RenderMode.REFERENCE_SCAN and reference_scan is not None
        else None
    )
    render = (
        reference_page_render(reference)
        if reference is not None
        else page_render_size(template, dpi)
    )

    # The population, when there is one, is the authority on how many scripts
    # exist - a cohort with absentees and missing scans produces fewer sheets
    # than it has candidates, and that difference is the point.
    sheets = population.sheets_to_render() if population is not None else ()
    planned_count = len(sheets) if population is not None else count
    if population is not None and planned_count < 1:
        raise ValueError(
            "This population produces no scripts at all - every candidate is "
            "absent or missing a scan, so there is nothing to render"
        )

    cases = plan_dataset(
        template,
        count=planned_count,
        seed=seed,
        profile=profile,
        custom_families=custom_families,
    )
    layout = FieldLayout.of(template)
    if population is not None:
        # Identity is overlaid onto the planned cases rather than planned
        # twice. See `attendance_dataset.bind_case` for why the two concerns
        # are split this way.
        cases = [
            bind_case(case, candidate, layout)
            for case, candidate in zip(cases, sheets, strict=True)
        ]

    # One canonical key per set, before anything is drawn. Candidates answer
    # against it, the solution sheets show it, the text files state it and the
    # manifest records it - four readers of one object, never four draws.
    key_sets, answer_sets = _answer_key_sets(cases, population, sheets, seed=seed)
    keys = generate_answer_keys(template, layout, key_sets, seed=seed)
    solution_files: dict[str, SolutionFiles] = {}
    if generate_solutions and keys:
        # Named now rather than after the candidate sheets are written, so two
        # set codes that sanitise to one file name are refused before any work.
        solution_files = solution_file_names(list(keys), chosen_format.suffix)
        for code in keys:
            if code and not set_code_markable(layout, code):
                _LOGGER.warning(
                    "Set code '%s' cannot be marked on this template's set-code "
                    "field; its solution sheet leaves the field blank, as the "
                    "candidate sheets for that set do",
                    code,
                )
    cases = apply_performance(cases, answer_sets, keys, layout, policy, seed=seed)

    # Read once for the whole run, never per sheet: the template's markers do
    # not move between sheets, and re-deriving them a hundred thousand times
    # would be the per-sheet reparsing this generator is built without.
    page_width, page_height = _canonical_page(template)
    outlines = marker_outlines(template, width=page_width, height=page_height)
    folds = fold_policy if fold_policy is not None else NO_FOLDS
    if folds.active:
        # Assigned after identity, so a folded sheet is still whoever the
        # population said it was - folding changes the paper, never the
        # candidate.
        cases = plan_folds(
            cases, folds, outlines, width=page_width, height=page_height, seed=seed
        )

    images = output_dir / IMAGES_DIRNAME
    truths = output_dir / GROUND_TRUTH_DIRNAME
    images.mkdir(parents=True, exist_ok=True)
    truths.mkdir(parents=True, exist_ok=True)

    encode_params = (
        [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)]
        if chosen_format is ImageFormat.JPEG
        else []
    )

    entries: list[str] = []
    rows: list[dict[str, Any]] = []
    cancelled = False

    _LOGGER.info(
        "Generating %d synthetic sheet(s): mode=%s colour=%s profile=%s seed=%d "
        "%dx%d %s (%s) format=%s",
        len(cases),
        render_mode.value,
        color_mode.value,
        profile.value,
        seed,
        render.width,
        render.height,
        f"@{render.dpi} dpi" if render.dpi > 0 else "native",
        render.derived_from,
        chosen_format.value,
    )

    for position, case in enumerate(cases, start=1):
        if should_cancel is not None and should_cancel():
            cancelled = True
            _LOGGER.info("Dataset generation cancelled after %d sheet(s)", position - 1)
            break

        sheet = render_case(
            template,
            case,
            render=render,
            image_format=chosen_format,
            prefix=prefix,
            dataset_version=version,
            seed=seed,
            render_mode=render_mode,
            reference=reference,
            color_mode=color_mode,
            outlines=outlines,
        )
        success, buffer = cv2.imencode(chosen_format.suffix, sheet.image, encode_params)
        if not success:  # pragma: no cover - encoding a valid array
            raise OSError(f"Could not encode {sheet.truth.scan}")
        (images / sheet.truth.scan).write_bytes(buffer.tobytes())

        truth_name = f"{Path(sheet.truth.scan).stem}.json"
        save_ground_truth(sheet.truth, truths / truth_name)
        entries.append(truth_name)
        rows.append(
            {
                "image": sheet.truth.scan,
                "ground_truth": truth_name,
                "tags": " ".join(sheet.truth.tags),
                "roll": sheet.truth.roll,
                "set_code": sheet.truth.set_code,
                "duplicate_group": sheet.truth.duplicate_group,
                "expect_failure": int(sheet.truth.expect_failure),
            }
        )
        if on_progress is not None:
            on_progress(GenerationProgress(position, len(cases), sheet.truth.scan))

    # A solution file from an earlier run into this folder must never survive
    # beside this run's - a leftover key looks exactly as valid as a new one.
    # Cleared even when solutions are off, for the same reason.
    solution_dir = output_dir / SOLUTION_DIRNAME
    clear_stale_solutions(solution_dir)
    solutions = (
        _write_solutions(
            solution_dir,
            template,
            keys,
            solution_files,
            layout=layout,
            render=render,
            image_format=chosen_format,
            encode_params=encode_params,
            dataset_version=version,
            seed=seed,
            render_mode=render_mode,
            reference=reference,
            color_mode=color_mode,
            outlines=outlines,
        )
        if solution_files
        else None
    )

    manifest = DatasetManifest(
        name=name,
        version=version,
        created_at=utc_timestamp(),
        template=template_path or template.name,
        generator={
            "generator_version": GENERATOR_VERSION,
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "seed": seed,
            "count": len(entries),
            "requested_count": count,
            "profile": profile.value,
            "families": [family.value for family in families_for(profile, custom_families)],
            "prefix": prefix,
            "template_id": template.template_id,
            "template_name": template.name,
            "template_version": template.format_version,
            # Null rather than the requested value when the pages came from a
            # real scan: the setting was not applied, and recording it as
            # though it were would describe a dataset nobody generated.
            "dpi": render.dpi if render.dpi > 0 else None,
            "page_pixels": [render.width, render.height],
            "page_size_from": render.derived_from,
            "render_mode": render_mode.value,
            "color_mode": color_mode.value,
            "reference_scan": reference.describe() if reference is not None else None,
            # Null when nothing was folded, matching the reference scan above:
            # a reader sees "this was not done" rather than a block of defaults
            # describing folds that never happened.
            "folds": folds.describe() if folds.active else None,
            # The canonical keys, whether or not their solution files were
            # written: they are what every candidate's intended answers were
            # decided against.
            "answer_keys": [key.describe() for key in keys.values()],
            "solutions": solutions,
            "performance": {
                **policy.describe(),
                "observed": summarise_scores(cases),
            },
            "image_format": chosen_format.value,
            "jpeg_quality": jpeg_quality if chosen_format is ImageFormat.JPEG else None,
            "cancelled": cancelled,
        },
        entries=tuple(entries),
        notes=_dataset_caveat(render_mode),
    )
    if population is not None:
        # Written after the images, and derived from the *plan* rather than
        # from anything that was rendered or recognised - see
        # `attendance_dataset`'s module docstring. A cancelled run still gets
        # them: the workbook describes the cohort, which did not stop existing
        # because the renderer was interrupted.
        write_workbooks(population, output_dir / ATTENDANCE_DIRNAME)
        write_ground_truth(population, truths)
        manifest.generator["attendance"] = summarise_population(population)

    save_manifest(manifest, output_dir / MANIFEST_FILENAME)

    if write_metadata:
        _write_manifest_csv(output_dir / MANIFEST_CSV_FILENAME, rows)
        _write_summary(output_dir / SUMMARY_FILENAME, manifest, rows)

    _LOGGER.info(
        "Dataset written: %d sheet(s) in %s%s",
        len(entries),
        output_dir,
        " (cancelled)" if cancelled else "",
    )
    return manifest


def _answer_key_sets(
    cases: Sequence[SheetCase],
    population: Population | None,
    sheets: Sequence[SyntheticCandidate],
    *,
    seed: int,
) -> tuple[tuple[str, ...], list[str]]:
    """Which sets get a key, and which key each case's candidate answered.

    With a roster, the sets are the roster's and a candidate answered the paper
    they are registered for - which is deliberately not always the set code
    marked on their sheet, since a wrong or blank set code is one of the
    staged conflicts.

    Without one, the sets are the ones the planned sheets carry. A sheet whose
    set code is a test case (blank, double-marked, faint) still sat *some*
    paper; which one is drawn from its own stream, so the choice moves nothing
    else. A template with no set-code field has one paper, keyed as ``""``.
    """
    if population is not None:
        return population.set_codes, [candidate.set_code for candidate in sheets]

    key_sets = planned_set_codes(cases) or ("",)
    rng = random.Random(f"{seed}:answer-set")
    answer_sets = [
        case.set_code if case.set_code in key_sets else rng.choice(list(key_sets))
        for case in cases
    ]
    return key_sets, answer_sets


def _write_solutions(
    directory: Path,
    template: OmrTemplate,
    keys: Mapping[str, SyntheticAnswerKey],
    files: Mapping[str, SolutionFiles],
    *,
    layout: FieldLayout,
    render: PageRender,
    image_format: ImageFormat,
    encode_params: Sequence[int],
    dataset_version: str,
    seed: int,
    render_mode: RenderMode,
    reference: ReferenceScan | None,
    color_mode: ColorMode,
    outlines: Sequence[MarkerOutline],
) -> dict[str, Any]:
    """Render and write every set's solution sheet, key text and ground truth.

    The solution sheet is an ordinary planned case drawn by :func:`render_case`
    - the same page, markers, bubbles, fill renderer, colour mode, resolution
    and reference scan as every candidate's script - with no degradation, so it
    serves as a clean reference.

    Returns:
        The manifest's ``solutions`` block.
    """
    import cv2

    directory.mkdir(parents=True, exist_ok=True)
    for index, (code, key) in enumerate(keys.items(), start=1):
        names = files[code]
        sheet = render_case(
            template,
            solution_case(key, layout, index=index),
            render=render,
            image_format=image_format,
            dataset_version=dataset_version,
            seed=seed,
            render_mode=render_mode,
            reference=reference,
            color_mode=color_mode,
            outlines=outlines,
        )
        success, buffer = cv2.imencode(image_format.suffix, sheet.image, list(encode_params))
        if not success:  # pragma: no cover - encoding a valid array
            raise OSError(f"Could not encode {names.sheet}")
        (directory / names.sheet).write_bytes(buffer.tobytes())
        (directory / names.answer_key).write_text(
            serialise_answer_key(key), encoding="utf-8", newline="\n"
        )
        truth = replace(
            sheet.truth,
            scan=names.sheet,
            metadata={
                **sheet.truth.metadata,
                "role": SOLUTION_ROLE,
                "answer_key": key.describe(),
                "answer_key_file": names.answer_key,
            },
        )
        save_ground_truth(truth, directory / names.ground_truth)
    _LOGGER.info("Solution sheets written: %d set(s) in %s", len(keys), directory)
    return describe_solutions(keys, files, layout)


def _dataset_caveat(render_mode: RenderMode) -> str:
    """Return the standing warning that belongs on a dataset made this way.

    Two different warnings because the two modes are honest about different
    things, and a dataset carrying the wrong one overstates or understates what
    it is worth. Neither says "validated": the marks are drawn by arithmetic in
    both modes, and a real candidate's pencil is not an ellipse.
    """
    if render_mode is RenderMode.REFERENCE_SCAN:
        return (
            "Synthetic marks on a real scanned blank sheet. The paper, printing, "
            "illumination and scanner behaviour are real; the marks are not. "
            "Measures recognition against real page conditions, and is still not "
            "a substitute for scans of real human-filled sheets."
        )
    return (
        "Synthetic data. Measures regression consistency and controlled "
        "edge-case handling; not evidence of real-world recognition accuracy."
    )


MANIFEST_COLUMNS: tuple[str, ...] = (
    "image",
    "ground_truth",
    "tags",
    "roll",
    "set_code",
    "duplicate_group",
    "expect_failure",
)
"""Columns of ``manifest.csv``. Stable: a spreadsheet is the second most
common way somebody looks at a dataset, after the images themselves."""


def _write_manifest_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write the per-sheet manifest as CSV."""
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def _write_summary(
    path: Path, manifest: DatasetManifest, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Write a dataset-level summary: how many sheets, and of what kinds."""
    tag_counts: dict[str, int] = {}
    for row in rows:
        for tag in str(row["tags"]).split():
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    duplicate_groups: dict[str, int] = {}
    for row in rows:
        group = str(row["duplicate_group"])
        if group:
            duplicate_groups[group] = duplicate_groups.get(group, 0) + 1

    payload = {
        "dataset": manifest.name,
        "dataset_version": manifest.version,
        "created_at": manifest.created_at,
        "generator": manifest.generator,
        "image_count": len(rows),
        "expected_failures": sum(int(row["expect_failure"]) for row in rows),
        "tag_counts": dict(sorted(tag_counts.items())),
        "duplicate_groups": {
            "groups": len(duplicate_groups),
            "sheets": sum(duplicate_groups.values()),
            "sizes": dict(sorted(duplicate_groups.items())),
        },
        "notes": manifest.notes,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def describe_template(template: OmrTemplate) -> dict[str, Any]:
    """Summarise what a template offers a generator, for a dialog or a log.

    Used to tell a user *before* they generate that their template has no set
    code, or eight identifier columns, rather than letting them discover it in
    the ground truth afterwards.
    """
    layout = FieldLayout.of(template)
    render = page_render_size(template)
    return {
        "name": template.name,
        "template_id": template.template_id,
        "identifier_columns": layout.identifier_columns,
        "identifier_symbols": len(layout.identifier_symbols),
        "set_code_columns": layout.set_columns,
        "set_code_symbols": len(layout.set_symbols),
        "questions": len(layout.questions),
        "options": len(layout.option_labels),
        "question_blocks": len(layout.question_zones),
        "page_pixels": [render.width, render.height],
        "dpi": render.dpi,
        "page_size_from": render.derived_from,
    }


def validate_template(template: OmrTemplate) -> tuple[str, ...]:
    """Return the reasons a template cannot be generated from, if any.

    An *optional* field that is missing is not a reason: a template with no
    set code simply produces no set-code cases. Only the things that make a
    sheet unrenderable are refusals.
    """
    problems: list[str] = []
    if not template.registration_markers:
        problems.append("the template declares no registration markers")
    if not any(zone.grid is not None for zone in template.zones):
        problems.append("the template declares no bubble grids")
    render = page_render_size(template)
    if render.width < 200 or render.height < 200:
        problems.append("the template's page is too small to render")
    return tuple(problems)


# Re-exported so that the many callers written against the previous module
# layout keep working: the names moved to `test_cases` and `case_plans` when
# the case taxonomy grew, but they are still part of this module's interface.
__all__ = [
    "CORNER_ROLES",
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_DPI",
    "DEFAULT_JPEG_QUALITY",
    "DEFAULT_PREFIX",
    "GENERATOR_VERSION",
    "GROUND_TRUTH_DIRNAME",
    "IMAGES_DIRNAME",
    "INDEPENDENT_FAILURE_TAGS",
    "MANIFEST_CSV_FILENAME",
    "MANIFEST_FILENAME",
    "MARKER_DEFECT_TAGS",
    "RENDER_FROM_REFERENCE_SCAN",
    "SOLUTION_DIRNAME",
    "SUMMARY_FILENAME",
    "UNDECLARED_DPI",
    "CaseFamily",
    "ColorMode",
    "DatasetProfile",
    "FoldCorner",
    "FoldPolicy",
    "FoldSeverity",
    "GeneratedSheet",
    "GenerationProgress",
    "ImageFormat",
    "MarkPlan",
    "PageRender",
    "ReferenceScan",
    "RenderMode",
    "SheetCase",
    "TestCaseTag",
    "describe_template",
    "generate_dataset",
    "load_reference_scan",
    "page_render_size",
    "plan_dataset",
    "reference_page_render",
    "render_case",
    "sheet_spec_from_template",
    "strip_unrenderable_defects",
    "validate_template",
]

