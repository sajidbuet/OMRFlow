"""Rendering the plan's images with the existing template-driven generator (revised phase 9).

Every :class:`~.cohort.Content` becomes one PNG in the campaign's *pool* - a
folder no source watches, which the writer processes copy from. The drawing is
:func:`omr_scanner.evaluation.synthetic_dataset.render_case` on a
:class:`~omr_scanner.evaluation.test_cases.SheetCase` built with the existing
:class:`~omr_scanner.evaluation.test_cases.SheetBuilder`; folds are
:mod:`omr_scanner.imaging.folds` folds solved by
:mod:`omr_scanner.evaluation.fold_plans`. A stray blank page is a white page of
the template's render size (one pixel varied, so two blank pages are two
distinct images).

Each rendered case is checked against the plan before it is written - the
bubbled roll, set and answers the builder derives must be the plan's - so a
planner error is caught here, as a harness defect, rather than surfacing later
as an apparent recognition failure.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.intake_qualification.cohort import (
    CampaignPlan,
    Content,
    ContentKind,
    IdDefect,
    answers_of,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.test_cases import SheetCase

POOL_INDEX = "pool_index.json"


@dataclass(frozen=True, slots=True)
class Rendered:
    """One pooled image."""

    key: str
    path: str
    size: int
    sha256: str


def build_case(plan: CampaignPlan, content: Content, template: OmrTemplate) -> SheetCase:
    """The :class:`SheetCase` that draws ``content`` (not for a blank page)."""
    from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder
    from omr_scanner.imaging.folds import FoldCorner, FoldSpec, severity_for_depth
    from omr_scanner.imaging.synthetic import DistortionSpec

    layout = FieldLayout.of(template)
    builder = SheetBuilder(layout, content.render_index, random.Random(content.render_index))
    builder.identifier(content.written_roll)
    if content.id_defect is IdDefect.BLANK:
        for column in range(layout.identifier_columns):
            builder.blank_identifier_column(column)
    elif content.id_defect is IdDefect.MULTIPLE:
        column = content.bubbled_roll.index("?")
        digit = content.written_roll[column]
        other = str((int(digit) + 5) % 10)
        builder.identifier_column(column, (digit, other))
    elif content.id_defect is IdDefect.WRONG:
        for column, character in enumerate(content.bubbled_roll):
            builder.identifier_column(column, (character,))
    builder.set_code(content.set_code)
    for number, value in zip(layout.questions, answers_of(plan, content), strict=True):
        if value:
            builder.answer(number, value.split("-"))
        else:
            builder.blank(number)
    folds: tuple[FoldSpec, ...] = ()
    if content.fold is not None:
        corner, depth_x, depth_y = content.fold
        folds = (FoldSpec(
            corner=FoldCorner(corner), severity=severity_for_depth(max(depth_x, depth_y)),
            depth_x=depth_x, depth_y=depth_y,
        ),)
    return builder.build(
        distortion=DistortionSpec(
            seed=content.render_index, rotation_degrees=content.rotation,
            translate_x_px=content.offset[0], translate_y_px=content.offset[1],
        ),
        folds=folds,
        notes=f"intake-qualification:{content.kind.value}:{content.key}",
    )


def check_case(plan: CampaignPlan, content: Content, case: SheetCase) -> None:
    """Refuse a drawn case that disagrees with the plan (a harness defect)."""
    if content.kind is ContentKind.FOLDED:
        return  # the fold hides the reading; the bubbles are still the candidate's
    if case.roll != content.bubbled_roll:
        raise ValueError(f"{content.key}: drew roll {case.roll!r}, plan {content.bubbled_roll!r}")
    if case.set_code != content.set_code:
        raise ValueError(f"{content.key}: drew set {case.set_code!r}, plan {content.set_code!r}")
    drawn = tuple(case.answers[number] for number in sorted(case.answers))
    if drawn != answers_of(plan, content):
        raise ValueError(f"{content.key}: drawn answers differ from the plan")


def render_bytes(plan: CampaignPlan, content: Content, template: OmrTemplate) -> bytes:
    """The PNG bytes of one content. Deterministic in the plan."""
    import cv2
    import numpy as np

    from omr_scanner.evaluation.synthetic_dataset import page_render_size, render_case

    if content.kind is ContentKind.BLANK_PAGE:
        size = page_render_size(template)
        image = np.full((size.height, size.width), 255, dtype=np.uint8)
        image[content.render_index % size.height, content.render_index % size.width] = 254
    else:
        case = build_case(plan, content, template)
        check_case(plan, content, case)
        image = render_case(template, case, seed=plan.config.seed).image
    ok, buffer = cv2.imencode(".png", image)
    if not ok:  # pragma: no cover - encoding a uint8 page does not fail
        raise RuntimeError(f"could not encode {content.key}")
    return bytes(buffer.tobytes())


# ----------------------------------------------------------------------
# Parallel rendering into the pool
# ----------------------------------------------------------------------
_WORKER: dict[str, Any] = {}


def _initialise(plan: CampaignPlan, template_path: str) -> None:
    from omr_scanner.services.template_service import load_template

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # pragma: no cover - best effort
        pass
    _WORKER["plan"] = plan
    _WORKER["template"] = load_template(Path(template_path))


def _render_one(key: str, pool: str) -> Rendered:
    plan: CampaignPlan = _WORKER["plan"]
    data = render_bytes(plan, plan.content(key), _WORKER["template"])
    path = Path(pool) / f"{key}.png"
    temporary = path.with_suffix(".png.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return Rendered(key=key, path=str(path), size=len(data),
                    sha256=hashlib.sha256(data).hexdigest())


def render_pool(
    plan: CampaignPlan,
    template_path: Path,
    pool: Path,
    *,
    workers: int,
    progress: Any = None,
) -> dict[str, Rendered]:
    """Render every content of ``plan`` into ``pool``; reuse a complete, matching pool.

    Returns ``{content key: Rendered}``. The index file records the plan
    digest; a pool rendered for another plan is never reused.
    """
    pool.mkdir(parents=True, exist_ok=True)
    index_path = pool / POOL_INDEX
    digest = plan.digest()
    if index_path.is_file():
        try:
            stored = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stored = {}
        if stored.get("plan_digest") == digest:
            found = {key: Rendered(**value) for key, value in stored.get("items", {}).items()}
            if len(found) == len(plan.contents) and all(
                Path(item.path).is_file() and Path(item.path).stat().st_size == item.size
                for item in found.values()
            ):
                return found
    keys = [item.key for item in plan.contents]
    rendered: dict[str, Rendered] = {}
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=max(1, workers), mp_context=context, initializer=_initialise,
        initargs=(plan, str(template_path)),
    ) as executor:
        for done, item in enumerate(
            executor.map(_render_one, keys, [str(pool)] * len(keys), chunksize=16), start=1
        ):
            rendered[item.key] = item
            if progress is not None and (done % 250 == 0 or done == len(keys)):
                progress(done, len(keys))
    owners: dict[str, str] = {}
    for key, item in rendered.items():
        if item.sha256 in owners:
            # Two distinct planned images with the same bytes would be a planted
            # "duplicate" nobody planned: a harness defect, refused here.
            raise ValueError(f"contents {owners[item.sha256]} and {key} rendered identical bytes")
        owners[item.sha256] = key
    index_path.write_text(
        json.dumps(
            {"plan_digest": digest,
             "items": {key: {"key": v.key, "path": v.path, "size": v.size, "sha256": v.sha256}
                       for key, v in rendered.items()}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return rendered


__all__ = ["POOL_INDEX", "Rendered", "build_case", "check_case", "render_bytes", "render_pool"]
