"""Generating OMR sheets whose correct answers are known.

Purpose:
    Produce test data for Phase 3 that is *reproducible* (a seed gives the same
    dataset every time), *labelled* (ground truth is written beside every
    image, never inferred afterwards), and *varied* (marks, geometry, exposure
    and structural damage, in controlled amounts).

Responsibilities:
    * :func:`sheet_spec_from_template` - project a real ``.omrt`` template onto
      a renderable synthetic page.
    * :func:`generate_dataset` - write ``images/``, ``ground_truth/`` and
      ``manifest.json`` for a whole dataset.
    * :class:`DatasetProfile` - how hard the sheets should be.

What does NOT belong here:
    * Recognition, or any judgement about it. This module knows what it *drew*;
      whether the engine agrees is :mod:`omr_scanner.evaluation.benchmark`'s
      question, and keeping the two apart is what stops the generator from
      quietly drawing whatever the engine happens to read.
    * Real candidate data. Roll numbers here are fictional by construction.

Why the template drives it:
    The sheets are rendered from the geometry of an actual template - its
    markers, its orientation mark, its zones' bubble grids - so a dataset
    exercises the coordinate mapping the engine really uses. A generator with
    its own private idea of where bubbles go would test the two halves of the
    application against each other's mistakes.

    That also means the same command generates a dataset for the repository's
    100-question sample sheet or for an institution's own template, which is
    what makes this useful when the real corpus arrives.

An honest warning, stated here because it is easy to forget:
    Synthetic accuracy is not real accuracy. These pages have clean geometry,
    even paper and marks drawn by arithmetic. They are excellent at catching
    *regressions* and coordinate bugs, and nearly useless as evidence that a
    threshold is right for real pencil on real paper.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.template import IgnoredFieldDefinition, QuestionBlockFieldDefinition
from omr_scanner.evaluation.ground_truth import (
    DatasetManifest,
    SheetGroundTruth,
    save_ground_truth,
    save_manifest,
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
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate, Zone

GENERATOR_VERSION = "1.0"
"""Version of this generator, recorded in every manifest.

Changing how a defect is drawn changes what a dataset means, so a benchmark
result that does not say which generator produced its data is not comparable
with anything."""

IMAGES_DIRNAME = "images"
GROUND_TRUTH_DIRNAME = "ground_truth"
MANIFEST_FILENAME = "manifest.json"

DEFAULT_PREFIX = "SYN"
"""Prefix for generated file names: ``SYN_000001.png``. Deliberately not
anything that could be mistaken for a real candidate's identifier."""


class DatasetProfile(StrEnum):
    """How much the generator is allowed to degrade a sheet.

    Four levels plus a mixture, rather than a continuum of knobs, because the
    question a developer asks is "does it still work on ordinary scans?" or
    "where does it break?" - not "what happens at blur 2.7".
    """

    CLEAN = "clean"
    """No distortion at all. Anything that fails here is a real bug, not a
    tolerance question."""

    NORMAL = "normal"
    """What a decent office scanner produces: a degree or two of rotation, mild
    exposure variation, light noise."""

    DIFFICULT = "difficult"
    """A tired photocopier and a hurried operator: noticeable skew and
    perspective, uneven illumination, blur, JPEG artefacts, and marks made in
    every style candidates actually use."""

    STRESS = "stress"
    """Deliberately at or past the documented limits, including structural
    damage - a missing marker, a cropped page. Most of these *should* fail, and
    the ground truth says so."""

    MIXED = "mixed"
    """A realistic batch: mostly normal, some difficult, a few broken."""


PROFILE_WEIGHTS: dict[DatasetProfile, dict[str, float]] = {
    DatasetProfile.CLEAN: {"clean": 1.0},
    DatasetProfile.NORMAL: {"normal": 1.0},
    DatasetProfile.DIFFICULT: {"difficult": 1.0},
    DatasetProfile.STRESS: {"difficult": 0.4, "broken": 0.6},
    DatasetProfile.MIXED: {"clean": 0.2, "normal": 0.5, "difficult": 0.25, "broken": 0.05},
}
"""How often each *case kind* appears under each profile.

Kept as data rather than branching code so that adding a profile is one entry,
and so a reader can see the whole policy at a glance."""


@dataclass(frozen=True, slots=True)
class GeneratedSheet:
    """One rendered sheet and the truth about it.

    Attributes:
        image: The rendered, distorted page.
        truth: What it should read.
    """

    image: NDArray[np.uint8]
    truth: SheetGroundTruth


@dataclass(frozen=True, slots=True)
class MarkPlan:
    """What is drawn into one response group.

    Attributes:
        labels: The symbols marked. Empty for a blank group; more than one for
            a deliberate double mark.
        fill: Coverage of each mark, in ``[0, 1]``.
        style: How the marks were made.
        intensity: How dark they are.
        offset_x: Horizontal displacement, in fractions of a bubble radius.
        offset_y: Vertical displacement.
        size_scale: Mark size multiplier.
        ambiguous: The mark was drawn deliberately borderline - too faint or
            too partial to be a fair "the engine must read this" assertion. The
            dataset records the question as ambiguous so that flagging it
            counts as correct behaviour, which it is.
    """

    labels: tuple[str, ...] = ()
    fill: float = 1.0
    style: MarkStyle = MarkStyle.FILL
    intensity: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    size_scale: float = 1.0
    ambiguous: bool = False

    @property
    def value(self) -> str:
        """The ground-truth value this plan produces (``""``, ``"B"``, ``"B-D"``)."""
        return "-".join(self.labels)


def sheet_spec_from_template(
    template: OmrTemplate,
    marks: Mapping[str, Mapping[int, MarkPlan]] | None = None,
    *,
    base: SyntheticSheetSpec | None = None,
    omit_markers: Sequence[str] = (),
    omit_orientation: bool = False,
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
            decoy graphics or a different marker size.
        omit_markers: Registration marker roles to leave off the page, for the
            structural failure cases.
        omit_orientation: Leave the orientation mark off.

    Returns:
        A specification whose page size, markers, orientation mark and bubbles
        all come from the template.
    """
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

    omitted = frozenset(MarkerRole(role) for role in omit_markers)
    return SyntheticSheetSpec(
        width=template.page.canonical_width_px,
        height=template.page.canonical_height_px,
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
        decoy_markers=start.decoy_markers,
        omit_markers=omitted,
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


# ----------------------------------------------------------------------
# Case construction
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _CaseRecipe:
    """One sheet's worth of decisions, before anything is drawn.

    Separated from the drawing so that the *what* (this sheet has a double mark
    on Q7 and is rotated 4 degrees) is decided in one place and can be recorded
    in the ground truth verbatim - which is what makes a failure reproducible.
    """

    kind: str
    distortion: DistortionSpec
    omit_markers: tuple[str, ...] = ()
    omit_orientation: bool = False
    expect_failure: bool = False
    defects: dict[str, Any] = field(default_factory=dict)


def _distortion_for(kind: str, rng: random.Random, seed: int) -> DistortionSpec:
    """Return the geometric and photometric degradation for one case kind.

    The numbers come from the limits ``docs/IMAGE_PROCESSING.md`` documents:
    "normal" stays comfortably inside them, "difficult" approaches them, and
    "broken" is past them on purpose.
    """
    if kind == "clean":
        return DistortionSpec(seed=seed)
    if kind == "normal":
        return DistortionSpec(
            rotation_degrees=rng.uniform(-2.0, 2.0),
            scale_x=rng.uniform(0.97, 1.03),
            scale_y=rng.uniform(0.97, 1.03),
            translate_x_px=rng.uniform(-15, 15),
            translate_y_px=rng.uniform(-15, 15),
            brightness_gain=rng.uniform(0.95, 1.05),
            noise_sigma=rng.uniform(0.0, 2.0),
            seed=seed,
        )
    if kind == "difficult":
        return DistortionSpec(
            rotation_degrees=rng.uniform(-7.0, 7.0),
            scale_x=rng.uniform(0.90, 1.10),
            scale_y=rng.uniform(0.90, 1.10),
            translate_x_px=rng.uniform(-40, 40),
            translate_y_px=rng.uniform(-40, 40),
            perspective_strength=rng.uniform(0.0, 0.02),
            brightness_gain=rng.uniform(0.85, 1.12),
            brightness_offset=rng.uniform(-15, 15),
            illumination_gradient=rng.uniform(0.0, 0.25),
            blur_kernel_px=rng.choice([0, 3, 5]),
            noise_sigma=rng.uniform(1.0, 6.0),
            jpeg_quality=rng.choice([None, 70, 50]),
            seed=seed,
        )
    # "broken": past the documented limits, structurally or geometrically.
    return DistortionSpec(
        rotation_degrees=rng.choice([-24.0, 24.0]),
        perspective_strength=rng.uniform(0.03, 0.05),
        margin_px=0,
        illumination_gradient=rng.uniform(0.4, 0.6),
        noise_sigma=rng.uniform(4.0, 10.0),
        seed=seed,
    )


def _recipe_for(kind: str, rng: random.Random, seed: int, index: int) -> _CaseRecipe:
    """Choose one sheet's degradation, including structural damage."""
    distortion = _distortion_for(kind, rng, seed)
    if kind != "broken":
        return _CaseRecipe(kind=kind, distortion=distortion, defects={"kind": kind})

    # Rotate through the structural failures rather than choosing randomly, so
    # a small "stress" dataset is guaranteed to contain one of each rather than
    # five copies of whichever the generator happened to roll.
    damage = index % 3
    if damage == 0:
        return _CaseRecipe(
            kind=kind,
            distortion=distortion,
            omit_markers=("top_right",),
            expect_failure=True,
            defects={"kind": kind, "missing_marker": "top_right"},
        )
    if damage == 1:
        return _CaseRecipe(
            kind=kind,
            distortion=distortion,
            omit_orientation=True,
            expect_failure=True,
            defects={"kind": kind, "missing_orientation_mark": True},
        )
    return _CaseRecipe(
        kind=kind,
        distortion=distortion,
        expect_failure=True,
        defects={"kind": kind, "extreme_rotation_degrees": distortion.rotation_degrees},
    )


def _mark_plan(kind: str, labels: Sequence[str], rng: random.Random) -> MarkPlan:
    """Choose how one answered question was marked.

    Clean sheets are marked the way the instructions ask. Harder ones use the
    styles candidates actually use - a tick, a cross, a ring, a light pencil, a
    mark half a radius off centre - because those are what a measurement has to
    survive, and they are invisible to a test that only ever draws a neat disc.
    """
    label = rng.choice(list(labels))
    if kind in {"clean", "normal"}:
        return MarkPlan(labels=(label,), fill=rng.uniform(0.85, 1.0))

    style = rng.choice(
        [MarkStyle.FILL, MarkStyle.FILL, MarkStyle.TICK, MarkStyle.CROSS,
         MarkStyle.SCRIBBLE, MarkStyle.RING]
    )
    return MarkPlan(
        labels=(label,),
        fill=rng.uniform(0.55, 1.0),
        style=style,
        intensity=rng.uniform(0.55, 1.0),
        offset_x=rng.uniform(-0.35, 0.35),
        offset_y=rng.uniform(-0.35, 0.35),
        size_scale=rng.uniform(0.75, 1.25),
    )


def _question_labels(zone: Zone) -> tuple[str, ...]:
    """Return the answer labels one question block offers."""
    field_definition = zone.field
    if isinstance(field_definition, QuestionBlockFieldDefinition):
        return tuple(field_definition.answer_labels)
    return ()


def _fictional_roll(rng: random.Random, digits: int) -> str:
    """Return a fictional roll number of the right length.

    Never drawn from anything resembling a real institution's numbering: the
    first digit is forced non-zero only so the value reads like an identifier,
    and the rest is noise. Committed fixtures must never carry a real
    candidate's number, and generating one by accident is easier than it
    sounds.
    """
    return "".join(str(rng.randint(0, 9)) for _ in range(digits))


def build_sheet(
    template: OmrTemplate,
    *,
    index: int,
    kind: str,
    seed: int,
    dataset_version: str = "1",
    prefix: str = DEFAULT_PREFIX,
) -> GeneratedSheet:
    """Render one labelled sheet.

    Args:
        template: The template to draw from.
        index: The sheet's position in the dataset, used in its file name.
        kind: ``clean``, ``normal``, ``difficult`` or ``broken``.
        seed: Seed for this sheet alone, so one sheet can be regenerated
            without regenerating the dataset around it.
        dataset_version: Recorded in the ground truth.
        prefix: File-name prefix.

    Returns:
        The rendered page and the truth about it.
    """
    rng = random.Random(seed)
    name = f"{prefix}_{index:06d}.png"
    recipe = _recipe_for(kind, rng, seed, index)

    marks: dict[str, dict[int, MarkPlan]] = {}
    answers: dict[int, str] = {}
    ambiguous: list[int] = []
    styles: dict[int, str] = {}
    roll = ""
    set_code = ""

    for zone in template.zones:
        if zone.grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        groups = list(zone_groups(zone))
        zone_plans: dict[int, MarkPlan] = {}

        if isinstance(zone.field, QuestionBlockFieldDefinition):
            labels = _question_labels(zone)
            first = zone.field.first_question
            for offset, group in enumerate(groups):
                number = first + offset
                plan = _answer_plan(kind, labels, rng)
                if plan is not None:
                    zone_plans[group.key] = plan
                    if plan.ambiguous:
                        ambiguous.append(number)
                    # Recorded per question, not per sheet: when a benchmark
                    # reports fifty false blanks, the first thing worth knowing
                    # is whether they were all ticks. Without this the analysis
                    # is guesswork.
                    styles[number] = plan.style.value
                answers[number] = plan.value if plan is not None else ""
        else:
            symbols = list(zone.field.symbols)
            value_characters: list[str] = []
            for group in groups:
                label = rng.choice(symbols)
                zone_plans[group.key] = MarkPlan(
                    labels=(label,), fill=rng.uniform(0.85, 1.0)
                )
                value_characters.append(label)
            value = "".join(value_characters)
            if zone.field.type.value == "numeric":
                roll = value
            elif zone.field.type.value == "set_code":
                set_code = value

        if zone_plans:
            marks[zone.id] = zone_plans

    spec = sheet_spec_from_template(
        template,
        marks,
        omit_markers=recipe.omit_markers,
        omit_orientation=recipe.omit_orientation,
    )
    sheet = render_sheet(spec)
    image = apply_distortion(sheet, recipe.distortion).image

    truth = SheetGroundTruth(
        scan=name,
        roll=roll,
        set_code=set_code,
        answers=answers,
        ambiguous=tuple(sorted(ambiguous)),
        expect_failure=recipe.expect_failure,
        dataset_version=dataset_version,
        metadata={
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "case_kind": kind,
            "defects": recipe.defects,
            "distortion": _distortion_summary(recipe.distortion),
            "mark_styles": styles,
        },
    )
    return GeneratedSheet(image=image, truth=truth)


def _answer_plan(kind: str, labels: Sequence[str], rng: random.Random) -> MarkPlan | None:
    """Decide what happens to one question: an answer, a blank, or two marks.

    The proportions are fixed rather than uniform because a dataset of random
    answers would contain almost no blanks and almost no double marks - the two
    cases the recognition rules exist for.
    """
    if not labels:
        return None
    roll = rng.random()
    if roll < 0.08:
        return None  # left blank
    if roll < 0.13:
        chosen = set(rng.sample(list(labels), k=min(2, len(labels))))
        # Sorted into *printed* order, not the order they were drawn: the
        # engine reports a double mark in the order the options appear on the
        # paper, and a ground truth that said "c-a" would fail a perfectly
        # correct reading of "a-c".
        pair = tuple(label for label in labels if label in chosen)
        return MarkPlan(labels=pair, fill=rng.uniform(0.8, 1.0))
    if kind in {"difficult", "broken"} and roll < 0.20:
        # A mark too faint to accept. The *correct* engine behaviour here is to
        # flag it rather than to read it, so the caller records the question as
        # deliberately ambiguous; see `SheetGroundTruth.ambiguous`.
        plan = _mark_plan(kind, labels, rng)
        return MarkPlan(
            labels=plan.labels,
            fill=rng.uniform(0.12, 0.28),
            style=plan.style,
            intensity=rng.uniform(0.3, 0.5),
            ambiguous=True,
        )
    return _mark_plan(kind, labels, rng)


def _distortion_summary(spec: DistortionSpec) -> dict[str, Any]:
    """Return the non-default distortion parameters, for the ground truth.

    Only what was actually applied: a record listing twenty defaults tells a
    reader nothing about which sheet they are looking at.
    """
    default = DistortionSpec()
    summary: dict[str, Any] = {}
    for name in (
        "rotation_degrees", "scale_x", "scale_y", "translate_x_px", "translate_y_px",
        "perspective_strength", "margin_px", "brightness_gain", "brightness_offset",
        "illumination_gradient", "blur_kernel_px", "noise_sigma", "jpeg_quality",
    ):
        value = getattr(spec, name)
        if value != getattr(default, name):
            summary[name] = value
    return summary


def generate_dataset(
    output_dir: Path,
    template: OmrTemplate,
    *,
    count: int = 24,
    seed: int = 20260918,
    profile: DatasetProfile = DatasetProfile.MIXED,
    name: str = "synthetic",
    version: str = "1",
    prefix: str = DEFAULT_PREFIX,
    template_path: str = "",
) -> DatasetManifest:
    """Write a complete labelled dataset to disk.

    Args:
        output_dir: Folder to create ``images/``, ``ground_truth/`` and
            ``manifest.json`` in.
        template: The template to render from.
        count: How many sheets.
        seed: Master seed. The same seed, count, profile and template produce
            the same dataset, which is what makes a benchmark comparable
            between two runs and two engine versions.
        profile: How hard the sheets should be.
        name: Dataset name, recorded in the manifest.
        version: Dataset revision.
        prefix: File-name prefix.
        template_path: Where the template came from, recorded in the manifest.

    Returns:
        The manifest, already written.

    Raises:
        ValueError: ``count`` is not positive, or the template declares no
            bubble grids to fill in.
    """
    import cv2

    if count < 1:
        raise ValueError(f"A dataset needs at least one sheet, got {count}")
    if not any(zone.grid is not None for zone in template.zones):
        raise ValueError(
            "This template declares no bubble grids, so there is nothing to generate"
        )

    images = output_dir / IMAGES_DIRNAME
    truths = output_dir / GROUND_TRUTH_DIRNAME
    images.mkdir(parents=True, exist_ok=True)
    truths.mkdir(parents=True, exist_ok=True)

    kinds = _case_kinds(profile, count, seed)
    entries: list[str] = []

    for index, kind in enumerate(kinds, start=1):
        # Each sheet gets its own derived seed, so regenerating sheet 7 alone
        # gives the same sheet 7, and changing `count` does not reshuffle the
        # sheets that were already there.
        sheet = build_sheet(
            template,
            index=index,
            kind=kind,
            seed=seed + index,
            dataset_version=version,
            prefix=prefix,
        )
        image_path = images / sheet.truth.scan
        success, buffer = cv2.imencode(".png", sheet.image)
        if not success:  # pragma: no cover - PNG encoding of a valid array
            raise OSError(f"Could not encode {sheet.truth.scan}")
        image_path.write_bytes(buffer.tobytes())

        truth_name = f"{image_path.stem}.json"
        save_ground_truth(sheet.truth, truths / truth_name)
        entries.append(truth_name)

    manifest = DatasetManifest(
        name=name,
        version=version,
        created_at=utc_timestamp(),
        template=template_path or template.name,
        generator={
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "count": count,
            "profile": profile.value,
            "prefix": prefix,
            "template_id": template.template_id,
        },
        entries=tuple(entries),
        notes=(
            "Synthetic data. Useful for regression and coordinate correctness; "
            "not evidence of real-world recognition accuracy."
        ),
    )
    save_manifest(manifest, output_dir / MANIFEST_FILENAME)
    return manifest


def _case_kinds(profile: DatasetProfile, count: int, seed: int) -> list[str]:
    """Return the case kind for each sheet, in order.

    Allocated proportionally and then shuffled with the dataset's own seed,
    rather than drawn independently per sheet: a twenty-sheet "mixed" dataset
    must reliably *contain* its one broken sheet, not merely have a five per
    cent chance of one per draw.
    """
    weights = PROFILE_WEIGHTS[profile]
    kinds: list[str] = []
    for kind, share in weights.items():
        kinds.extend([kind] * max(round(share * count), 1 if share > 0 else 0))

    # Rounding can overshoot or undershoot; trim or pad with the commonest kind.
    commonest = max(weights, key=lambda key: weights[key])
    while len(kinds) < count:
        kinds.append(commonest)
    del kinds[count:]

    random.Random(seed).shuffle(kinds)
    return kinds


__all__ = [
    "GENERATOR_VERSION",
    "GROUND_TRUTH_DIRNAME",
    "IMAGES_DIRNAME",
    "MANIFEST_FILENAME",
    "DatasetProfile",
    "GeneratedSheet",
    "MarkPlan",
    "build_sheet",
    "generate_dataset",
    "sheet_spec_from_template",
]
