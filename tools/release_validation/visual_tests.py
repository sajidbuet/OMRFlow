"""Stage: visual regression, compared with tolerance and never on its own.

How this fits with the GUI stage:
    The GUI suite takes the screenshots, as a side effect of the functional
    checks it was already performing. This stage compares them with the
    committed baselines in ``tools/release_validation/baselines/``. Nothing
    functional depends on the comparison: §6 of the brief is explicit that a
    functional failure must not hinge on pixel equality, and a layout change
    somebody meant to make should not be reported as a broken release.

Why a tolerance, and what shape it has:
    Two screenshots of the same interface differ for reasons that are not
    defects - font hinting, anti-aliasing, the display scale factor, a
    different GPU compositing the same widget. So a pixel counts as changed
    only when it differs by more than :data:`CHANNEL_TOLERANCE`, and an image
    is reported as changed only when more than :data:`AREA_TOLERANCE` of its
    pixels do. A size change is reported outright, because that is a layout
    change rather than a rendering difference.

Baselines are never updated silently:
    An ordinary run compares and reports. ``--update-visual-baselines`` copies
    the current screenshots over the baselines and says so, which is the only
    way they change.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.results import StageResult, Status

CHANNEL_TOLERANCE = 12
"""Per-channel difference, 0-255, below which a pixel counts as unchanged."""

AREA_TOLERANCE = 0.015
"""Fraction of differing pixels an image may have before it is reported.

1.5%: enough to absorb text re-hinting across a dialog, far too little to hide
a moved button or a missing icon.
"""


def _compare(baseline: Path, candidate: Path) -> tuple[bool, str]:
    """Return ``(matches, description)`` for one image pair."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - reported by the caller
        return True, "Pillow is not installed"

    with Image.open(baseline) as first, Image.open(candidate) as second:
        if first.size != second.size:
            return False, f"size changed: {first.size} -> {second.size}"
        left = first.convert("RGB")
        right = second.convert("RGB")

        # Done with numpy when it is there - these are megapixel images and a
        # Python loop over them would dominate the whole qualification run.
        try:
            import numpy as np

            a = np.asarray(left, dtype=np.int16)
            b = np.asarray(right, dtype=np.int16)
            delta = np.abs(a - b).max(axis=2)
            changed = int((delta > CHANNEL_TOLERANCE).sum())
            total = int(delta.size)
        except ImportError:  # pragma: no cover - numpy is a runtime dependency
            from PIL import ImageChops

            difference = ImageChops.difference(left, right).convert("L")
            histogram = difference.histogram()
            changed = sum(
                count
                for value, count in enumerate(histogram)
                if value > CHANNEL_TOLERANCE
            )
            total = left.size[0] * left.size[1]

    fraction = changed / total if total else 0.0
    if fraction > AREA_TOLERANCE:
        return False, f"{fraction:.2%} of pixels differ (tolerance {AREA_TOLERANCE:.2%})"
    return True, f"{fraction:.2%} of pixels differ"


def run(config: cfg.ValidationConfig) -> StageResult:
    """Compare this run's screenshots against the committed baselines."""
    started = time.monotonic()
    # Non-blocking: a deliberate redesign must not fail a release, and a
    # genuine regression shows up in the functional stages as well.
    stage = StageResult(name="Visual regression", blocking=False)

    try:
        import PIL  # noqa: F401
    except ImportError:
        stage.skipped_reason = "Pillow is not installed (pip install pillow)"
        return stage

    screenshots = sorted(config.screenshots_dir.glob("*.png"))
    if not screenshots:
        stage.skipped_reason = (
            "no screenshots were captured - the GUI stage did not run, or ran "
            "on a headless platform"
        )
        return stage

    baselines = cfg.BASELINE_DIR
    baselines.mkdir(parents=True, exist_ok=True)

    if config.update_visual_baselines:
        for shot in screenshots:
            shutil.copy2(shot, baselines / shot.name)
            stage.record(
                f"baseline updated: {shot.name}",
                Status.WARNING,
                detail=str((baselines / shot.name).relative_to(cfg.REPOSITORY_ROOT)),
                reason="--update-visual-baselines was given; review the diff before committing",
            )
        stage.duration_seconds = time.monotonic() - started
        return stage

    for shot in screenshots:
        baseline = baselines / shot.name
        if not baseline.is_file():
            stage.record(
                f"baseline exists: {shot.name}",
                Status.WARNING,
                detail=f"screenshots/{shot.name}",
                reason=(
                    "no committed baseline for this checkpoint; run with "
                    "--update-visual-baselines to create one"
                ),
                artifacts=[f"screenshots/{shot.name}"],
            )
            continue

        matches, description = _compare(baseline, shot)
        stage.record(
            f"visual: {shot.stem}",
            Status.PASS if matches else Status.WARNING,
            detail=description,
            reason="" if matches else f"{description}; compare with {baseline.name}",
            artifacts=[f"screenshots/{shot.name}"],
        )

    stage.duration_seconds = time.monotonic() - started
    return stage
