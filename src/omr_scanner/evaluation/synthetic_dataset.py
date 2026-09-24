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
      cancellation.

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

Resolution:
    Sheets are rendered at :data:`DEFAULT_DPI` from the template's *physical*
    page size in millimetres, not at its canonical pixel size. That is the
    honest thing to do - a real scan is whatever the scanner produced, and
    rendering at the canonical size would hand the engine a page that needed
    no rescaling and quietly stop testing one.

An honest warning, stated here because it is easy to forget:
    Synthetic accuracy is not real accuracy. These pages have clean geometry,
    even paper and marks drawn by arithmetic. They measure *regression
    consistency and controlled edge-case handling*, and are nearly useless as
    evidence that a threshold is right for real pencil on real paper.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.template import IgnoredFieldDefinition
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
from omr_scanner.evaluation.ground_truth import (
    DatasetManifest,
    SheetGroundTruth,
    save_ground_truth,
    save_manifest,
)
from omr_scanner.evaluation.test_cases import (
    CaseFamily,
    FieldLayout,
    MarkPlan,
    SheetCase,
    TestCaseTag,
)
from omr_scanner.imaging.synthetic import (
    AnswerBubbleSpec,
    DistortionSpec,
    MarkStyle,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)
from omr_scanner.recognition.fields import zone_groups
from omr_scanner.services.recognition_models import utc_timestamp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)

GENERATOR_VERSION = "2.0"
"""Version of this generator, recorded in every manifest.

Changing how a defect is drawn changes what a dataset means, so a benchmark
result that does not say which generator produced its data is not comparable
with anything. Bumped to 2.0 when named test cases, DPI-based rendering and
JPEG output arrived."""

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


def render_case(
    template: OmrTemplate,
    case: SheetCase,
    *,
    render: PageRender | None = None,
    image_format: ImageFormat = ImageFormat.PNG,
    prefix: str = DEFAULT_PREFIX,
    dataset_version: str = "1",
    seed: int = 0,
) -> GeneratedSheet:
    """Render one planned case and return it with its ground truth.

    Args:
        template: The template to draw from.
        case: What this sheet is, from
            :func:`~omr_scanner.evaluation.case_plans.plan_dataset`.
        render: Pixel size; derived from the template at
            :data:`DEFAULT_DPI` when omitted.
        image_format: Decides the file name recorded in the ground truth.
        prefix: File-name prefix.
        dataset_version: Recorded in the ground truth.
        seed: The dataset's master seed, recorded so one sheet can be
            reproduced on its own.

    Returns:
        The image and the ground truth, which is copied from the case rather
        than re-derived - the case already holds the single statement of what
        was drawn.
    """
    size = render if render is not None else page_render_size(template)
    name = f"{prefix}_{case.index:06d}{image_format.suffix}"

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
    image = apply_distortion(sheet, case.distortion).image

    truth = SheetGroundTruth(
        scan=name,
        roll=case.roll,
        set_code=case.set_code,
        answers=dict(case.answers),
        ambiguous=case.ambiguous_questions,
        expect_failure=case.expect_failure,
        tags=case.tag_values,
        roll_marks=case.roll_marks,
        roll_ambiguous=case.roll_ambiguous,
        set_marks=case.set_marks,
        set_ambiguous=case.set_ambiguous,
        duplicate_group=case.duplicate_group,
        degradation=_degradation_summary(case),
        notes=case.notes,
        dataset_version=dataset_version,
        metadata={
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "case_index": case.index,
            "render": {
                "width": size.width,
                "height": size.height,
                "dpi": size.dpi,
                "derived_from": size.derived_from,
            },
            "mark_styles": _mark_styles(case),
        },
    )
    return GeneratedSheet(image=image, truth=truth)


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
        on_progress: Called after each sheet is written. Runs on the calling
            thread, so a GUI caller must marshal to the main thread.
        should_cancel: Polled before each sheet; returning ``True`` stops the
            run and writes a manifest describing what was actually produced.

    Returns:
        The manifest, already written.

    Raises:
        ValueError: ``count`` is not positive, the format is unsupported, or
            the template declares no bubble grids to fill in.

    Memory:
        One sheet is rendered, encoded, written and released before the next
        begins. A dataset of ten thousand sheets therefore costs one page of
        memory, not ten thousand.
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

    render = page_render_size(template, dpi)

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
    if population is not None:
        # Identity is overlaid onto the planned cases rather than planned
        # twice. See `attendance_dataset.bind_case` for why the two concerns
        # are split this way.
        layout = FieldLayout.of(template)
        cases = [
            bind_case(case, candidate, layout)
            for case, candidate in zip(cases, sheets, strict=True)
        ]

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
        "Generating %d synthetic sheet(s): profile=%s seed=%d %dx%d @%d dpi (%s) format=%s",
        len(cases),
        profile.value,
        seed,
        render.width,
        render.height,
        render.dpi,
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
            "dpi": render.dpi,
            "page_pixels": [render.width, render.height],
            "page_size_from": render.derived_from,
            "image_format": chosen_format.value,
            "jpeg_quality": jpeg_quality if chosen_format is ImageFormat.JPEG else None,
            "cancelled": cancelled,
        },
        entries=tuple(entries),
        notes=(
            "Synthetic data. Measures regression consistency and controlled "
            "edge-case handling; not evidence of real-world recognition accuracy."
        ),
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
    "MANIFEST_CSV_FILENAME",
    "MANIFEST_FILENAME",
    "SUMMARY_FILENAME",
    "CaseFamily",
    "DatasetProfile",
    "GeneratedSheet",
    "GenerationProgress",
    "ImageFormat",
    "MarkPlan",
    "PageRender",
    "SheetCase",
    "TestCaseTag",
    "describe_template",
    "generate_dataset",
    "page_render_size",
    "plan_dataset",
    "render_case",
    "sheet_spec_from_template",
    "validate_template",
]

