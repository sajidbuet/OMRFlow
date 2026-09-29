"""Old candidate marks on a reference scan: finding them and taking them off.

Purpose:
    A "blank" reference is often a used script - on this repository's own
    sample every answer, every identifier ``0`` and both set-code positions are
    filled. Laid under every synthetic sheet, those marks would contradict the
    sheet's ground truth: the truth says question 1 is ``B``, the paper shows
    ``A`` filled as well. So, once per run and before any sheet is drawn, every
    bubble the template declares is checked and any candidate ink in it is
    removed.

How a filled bubble is repaired, and why this way:
    A filled bubble has lost its printed label and much of its printed ring
    under the ink; no local fill can bring those back. But the same form prints
    the same bubble many times over - every question's ``a``, every identifier
    column's ``3`` - and nearly all of them are unfilled. So, per bubble:

    1. **Exemplar.** The lightest *other* bubble printed with the same label at
       the same size - from the same field where there is one, since a field is
       printed in one pass at one weight and tint - provided it looks like the
       form's typical unfilled bubble. A label every copy of which is filled
       has no exemplar rather than a filled one.
    2. **Is there ink?** Judged only where the exemplar is bare paper, well
       clear of its printing. Printing varies across a page (one block of the
       sample is printed visibly heavier than another) and a heavier-printed
       but unfilled bubble must not look inked; a fill, a tick or a cross
       always covers paper. The first test tolerates any misregistration
       within :data:`_SHIFT`, so an unfilled bubble stops there, untouched, and
       a clean reference comes out byte-identical.
    3. **Repair.** The exemplar is aligned to the bubble on the pixels the ink
       has not reached, and only the ink pixels (grown to take a stroke's soft
       edge) are replaced with the exemplar's, scaled to the local paper colour.
       Ring, label and paper grain come back from the scan itself, in its own
       print colour and texture.
    4. **No exemplar.** Only then is an unmistakable fill inpainted from its
       surroundings; its label is lost, and counted.

    Everything happens in scan pixels around each bubble's own centre - nothing
    is resampled - and only once per run, never per sheet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from omr_scanner.domain.template import IgnoredFieldDefinition
from omr_scanner.recognition.fields import zone_groups

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)

_PATCH_REACH = 1.8
"""How far around a bubble its patch reaches, in radii, on a side with no
other bubble near - far enough to take a scribble that overflows the ring.
Towards another bubble it stops just short of that bubble's ring."""

_SHIFT = 5
"""Largest misregistration between two bubbles' printing, in scan pixels, that
the comparison tolerates. Measured on the sample: up to 4 px across a page."""

_DARK_RATIO = 0.60
"""For choosing exemplars: a pixel darker than this share of the paper is ink
or heavy print."""

_EXEMPLAR_SLACK = 0.20
"""An exemplar may be this much darker (share of its disc) than the form's
typical unfilled bubble and still count as unfilled."""

_SIZE_TOLERANCE = 0.15
"""Bubbles within this share of each other's radius are the same size."""

_BARE_RATIO = 0.85
"""Where the exemplar - even at its darkest nearby pixel - is at least this
share of the paper level, it is bare paper."""

_INK_DELTA = 0.22
"""Ink: darker than the exemplar's darkest nearby pixel by this share of the
paper level. Measured on the sample: paper ~245, pale pink print ~140, fills
30-110, tint differences between shaded and unshaded columns ~15."""

_CONTAMINATED = 0.04
"""Share of a bubble's bare paper that must be ink before it is repaired."""

_OWN_PRINT = 1.05
"""Within this many radii of its centre, a bubble's printing is its own ring
and label - identical to its exemplar's. Beyond, it may not be."""

_HALO = 2
"""Pixels of a stroke's soft edge left outside its mask, which a fill beside
it steps over to reach clean paper or print."""

_NEIGHBOUR = 3.2
"""Bubbles whose centres are within this many radii are neighbours - the
next bubble along a row or column, and diagonally."""

_OVERFLOW_PIXELS = 6
"""On an unfilled neighbour of a repaired bubble, this many ink pixels are
enough to remove."""

_FILL_RATIO = 0.45
"""Without an exemplar: a pixel darker than this share of the paper is fill -
below the sample's pink print (~0.57 of paper), so a pale ring survives."""


@dataclass(frozen=True, slots=True)
class BubbleCleanup:
    """What :func:`clean_reference_bubbles` found and did, for the manifest.

    Attributes:
        examined: Bubbles checked.
        cleaned: Bubbles that held candidate ink and were repaired.
        cleaned_by_zone: ``(zone id, bubbles repaired)``, zones with any.
        labels_not_restored: Repaired bubbles whose label had no unfilled copy
            anywhere on the form, so were inpainted and lost their label.
        skipped: Bubbles too near the image edge to examine.
        overflow_cleaned: Unfilled bubbles beside a repaired one from which a
            fill's overflow was removed.
    """

    examined: int
    cleaned: int
    cleaned_by_zone: tuple[tuple[str, int], ...]
    labels_not_restored: int
    skipped: int
    overflow_cleaned: int = 0

    def describe(self) -> dict[str, object]:
        """As JSON-safe plain data."""
        return {
            "examined": self.examined,
            "cleaned": self.cleaned,
            "cleaned_by_zone": dict(self.cleaned_by_zone),
            "labels_not_restored": self.labels_not_restored,
            "skipped": self.skipped,
            "overflow_cleaned": self.overflow_cleaned,
        }


@dataclass(frozen=True, slots=True)
class _Bubble:
    """One declared bubble, in scan pixels."""

    zone_id: str
    label: str
    x: int
    y: int
    radius: float
    reach: Reach
    darkness: float = 0.0
    stray: int = 0

    @property
    def half(self) -> int:
        """The square patch that fits within every side's reach."""
        return min(self.reach)


Reach = tuple[int, int, int, int]
"""How far a patch extends from a bubble's centre: left, right, up, down."""


def clean_reference_bubbles(
    image: NDArray[np.uint8],
    canonical_to_scan: NDArray[np.float64],
    template: OmrTemplate,
    *,
    canonical_width: int,
    canonical_height: int,
) -> tuple[NDArray[np.uint8], BubbleCleanup]:
    """Remove old candidate marks from every bubble the template declares.

    Args:
        image: The reference scan, grey or BGR. Read only.
        canonical_to_scan: Homography from canonical page pixels to ``image``.
        template: Declares where the bubbles are and what each is printed with.
        canonical_width: Canonical page width the template is normalised to.
        canonical_height: Canonical page height.

    Returns:
        The cleaned image (a new array) and a record of what was done.
    """
    grey = (
        image if image.ndim == 2
        else np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)
    )
    located = _locate(template, canonical_to_scan, canonical_width, canonical_height)
    height, width = grey.shape
    margin = _SHIFT + 1
    bubbles = [
        _with_darkness(grey, b) for b in located
        if b.x - b.reach[0] - margin >= 0 and b.x + b.reach[1] + margin < width
        and b.y - b.reach[2] - margin >= 0 and b.y + b.reach[3] + margin < height
    ]
    skipped = len(located) - len(bubbles)
    typical = float(np.percentile([b.darkness for b in bubbles], 20)) if bubbles else 0.0
    by_label: dict[str, list[_Bubble]] = {}
    for bubble in sorted(bubbles, key=lambda b: (b.stray, b.darkness)):
        by_label.setdefault(bubble.label, []).append(bubble)

    result = image.copy()
    cleaned: dict[str, int] = {}
    repaired_at: list[_Bubble] = []
    lost = 0
    for bubble in bubbles:
        exemplar = _exemplar_for(bubble, by_label.get(bubble.label, []), typical)
        if exemplar is not None:
            repaired = _repair_from(image, grey, result, bubble, exemplar)
        else:
            repaired = _inpaint(image, grey, result, bubble)
            lost += int(repaired)
        if repaired:
            cleaned[bubble.zone_id] = cleaned.get(bubble.zone_id, 0) + 1
            repaired_at.append(bubble)

    # A fill that overflowed its own bubble ends on the ring of the unfilled
    # one beside it - a sliver far too small a share of that bubble to count
    # as a mark. So each neighbour of a repaired bubble is looked at again and
    # any ink in it at all is taken; ink still meaning darker than its aligned
    # exemplar's printing, so a heavier print is not.
    done = {id(b) for b in repaired_at}
    overflow = 0
    for bubble in bubbles:
        if id(bubble) in done or not any(
            np.hypot(bubble.x - other.x, bubble.y - other.y) <= _NEIGHBOUR * bubble.radius
            for other in repaired_at
        ):
            continue
        exemplar = _exemplar_for(bubble, by_label.get(bubble.label, []), typical)
        if exemplar is not None and _repair_from(
            image, grey, result, bubble, exemplar, overflow_only=True
        ):
            overflow += 1

    record = BubbleCleanup(
        examined=len(bubbles),
        cleaned=sum(cleaned.values()),
        cleaned_by_zone=tuple(sorted(cleaned.items())),
        labels_not_restored=lost,
        skipped=skipped,
        overflow_cleaned=overflow,
    )
    _LOGGER.info(
        "Reference bubbles: %d examined, %d held old marks and were cleaned "
        "(%d without a clean copy of their label), %d skipped at the edge",
        record.examined, record.cleaned, record.labels_not_restored, record.skipped,
    )
    return result, record


def _locate(
    template: OmrTemplate,
    canonical_to_scan: NDArray[np.float64],
    canonical_width: int,
    canonical_height: int,
) -> list[_Bubble]:
    """Every declared bubble, in scan pixels, with its printed label."""
    raw: list[tuple[str, str, float, float, float]] = []
    for zone in template.zones:
        grid = zone.grid
        if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        radius = min(grid.bubble_size.width * canonical_width,
                     grid.bubble_size.height * canonical_height) / 2.0
        for group in zone_groups(zone):
            for cell, label in zip(group.cells, group.labels, strict=True):
                centre = grid.bubble_center(*cell)
                raw.append((zone.id, label, centre.x * canonical_width,
                            centre.y * canonical_height, radius))
    if not raw:
        return []
    points = np.array([[[x, y]] for _z, _l, x, y, _r in raw], dtype=np.float64)
    placed = cv2.perspectiveTransform(points, canonical_to_scan).reshape(-1, 2)
    # The homography's local scale at each bubble, from a one-pixel step.
    stepped = cv2.perspectiveTransform(points + np.array([1.0, 0.0]), canonical_to_scan)
    scale = np.linalg.norm(stepped.reshape(-1, 2) - placed, axis=1)

    located = []
    for index, ((zone_id, label, _x, _y, radius), (x, y)) in enumerate(
        zip(raw, placed, strict=True)
    ):
        r = radius * float(scale[index])
        # Each side reaches up to just short of the ring of the nearest bubble
        # on that side - the strip between two rings is where an overflowing
        # fill ends up, and each of the two cleans its own side of it - and
        # the full reach where there is none, as beyond a row's last bubble.
        dx, dy = placed[:, 0] - x, placed[:, 1] - y
        gap = np.hypot(dx, dy) - r - 2.0
        gap[index] = np.inf
        across = np.abs(dx) >= np.abs(dy)
        sides = (across & (dx < 0), across & (dx > 0), ~across & (dy < 0), ~across & (dy > 0))
        left, right, up, down = (
            int(min(_PATCH_REACH * r, float(gap[side].min()) if side.any() else np.inf))
            for side in sides
        )
        if min(left, right, up, down) >= 2:
            located.append(
                _Bubble(zone_id, label, round(x), round(y), r, (left, right, up, down))
            )
    return located


def _square(half: int) -> Reach:
    return half, half, half, half


def _grown(reach: Reach, by: int) -> Reach:
    return reach[0] + by, reach[1] + by, reach[2] + by, reach[3] + by


def _disc(reach: Reach, radius: float) -> NDArray[np.bool_]:
    left, right, up, down = reach
    yy, xx = np.mgrid[-up : down + 1, -left : right + 1]
    return np.asarray(xx * xx + yy * yy <= radius * radius)


def _paper(patch: NDArray[np.uint8]) -> float:
    """The brightness of bare paper in a patch - a high percentile, not the max."""
    return max(float(np.percentile(patch, 90)), 1.0)


def _patch(image: NDArray[np.uint8], bubble: _Bubble, reach: Reach, dx: int = 0, dy: int = 0,
           ) -> NDArray[np.uint8]:
    left, right, up, down = reach
    x, y = bubble.x + dx, bubble.y + dy
    return image[y - up : y + down + 1, x - left : x + right + 1]


def _with_darkness(grey: NDArray[np.uint8], bubble: _Bubble) -> _Bubble:
    """The bubble, with how inked it looks.

    ``darkness`` is the share of its inner disc that is ink or heavy print -
    what says whether it is filled. ``stray`` counts the pixels anywhere in its
    patch darker than any pale printing - what says whether a neighbour's
    fill has spilled onto it, which must rule it out as an exemplar even
    though its own disc is clean.
    """
    square = _square(bubble.half)
    patch = _patch(grey, bubble, square)
    disc = _disc(square, 0.85 * bubble.radius)
    paper = _paper(patch)
    darkness = float(np.mean(patch[disc] < _DARK_RATIO * paper))
    stray = int(np.count_nonzero(_patch(grey, bubble, bubble.reach) < _FILL_RATIO * paper))
    return _Bubble(bubble.zone_id, bubble.label, bubble.x, bubble.y, bubble.radius,
                   bubble.reach, darkness, stray)


def _exemplar_for(
    bubble: _Bubble, same_label: Sequence[_Bubble], typical: float
) -> _Bubble | None:
    """The clean appearance of ``bubble``, or ``None`` if the form has none.

    Cleanest first (``same_label`` is sorted so), preferring - in this order -
    the same field *and* a patch reaching as far on every side as the
    bubble's own (a copy in the same column of a table, which has the same
    table line beside it); the same field; any field with that reach; any.
    """
    candidates = [
        other for other in same_label
        if other is not bubble
        and abs(other.radius - bubble.radius) <= _SIZE_TOLERANCE * bubble.radius
        and other.darkness <= typical + _EXEMPLAR_SLACK
    ]

    def covers(other: _Bubble) -> bool:
        return all(a >= b for a, b in zip(other.reach, bubble.reach, strict=True))

    same_zone = [other for other in candidates if other.zone_id == bubble.zone_id]
    for pool in (
        [o for o in same_zone if covers(o)], same_zone,
        [o for o in candidates if covers(o)], candidates,
    ):
        if pool:
            return pool[0]
    return None


def _repair_from(
    image: NDArray[np.uint8],
    grey: NDArray[np.uint8],
    result: NDArray[np.uint8],
    bubble: _Bubble,
    exemplar: _Bubble,
    *,
    overflow_only: bool = False,
) -> bool:
    """Replace the ink in ``bubble`` with ``exemplar``'s pixels; ``True`` if any.

    ``overflow_only`` is for an unfilled neighbour of a repaired bubble: no
    share of the disc need be inked, only :data:`_OVERFLOW_PIXELS` anywhere in
    the patch once aligned.
    """
    # No further than either bubble reaches, so neither brings in the ring of
    # a neighbour the other does not have.
    mine, theirs = bubble.reach, exemplar.reach
    reach: Reach = (
        min(mine[0], theirs[0]), min(mine[1], theirs[1]),
        min(mine[2], theirs[2]), min(mine[3], theirs[3]),
    )
    rows, columns = reach[2] + reach[3] + 1, reach[0] + reach[1] + 1
    target = _patch(grey, bubble, reach).astype(np.int16)
    wide = _patch(grey, exemplar, _grown(reach, _SHIFT))
    paper = _paper(wide)
    delta = _INK_DELTA * paper
    disc = _disc(reach, bubble.radius)

    def at(dx: int, dy: int) -> NDArray[np.int16]:
        return wide[_SHIFT + dy : _SHIFT + dy + rows, _SHIFT + dx : _SHIFT + dx + columns].astype(
            np.int16
        )

    # The cheap test, tolerant of any misregistration within the search: ink
    # is darker than the exemplar's darkest pixel anywhere within reach - its
    # printing included, which a fill is and a heavier print of the same
    # label is not.
    loose = cv2.erode(wide, np.ones((2 * _SHIFT + 1,) * 2, np.uint8))
    loose = loose[_SHIFT : _SHIFT + rows, _SHIFT : _SHIFT + columns].astype(np.int16)
    loose_ink = target < loose - delta
    if overflow_only:
        if np.count_nonzero(loose_ink) < _OVERFLOW_PIXELS:
            return False
    elif float(np.mean(loose_ink[disc])) < _CONTAMINATED:
        return False

    # Align on what the ink has not reached - a saturating difference, so the
    # fill itself cannot vote - then test again, now tightly.
    costs = {
        (dx, dy): float(np.minimum(np.abs(target - at(dx, dy)), 40).sum())
        for dy in range(-_SHIFT, _SHIFT + 1)
        for dx in range(-_SHIFT, _SHIFT + 1)
    }
    offset = min(costs, key=lambda key: (costs[key], abs(key[0]) + abs(key[1])))
    aligned = at(*offset).astype(np.uint8)
    near = cv2.erode(aligned, np.ones((5, 5), np.uint8)).astype(np.int16)
    tight = cv2.erode(aligned, np.ones((3, 3), np.uint8)).astype(np.int16)
    # Inside the ring the bubble's printing is exactly the exemplar's, so ink
    # is whatever is darker than the exemplar. Outside it, the two may differ
    # - a table or frame line beside one copy and not the other - so ink must
    # also be darker than this form's printing, or a line would be "cleaned".
    own = _disc(reach, _OWN_PRINT * bubble.radius)
    printed = aligned[aligned < _BARE_RATIO * paper]
    print_level = float(np.median(printed)) if printed.size else 0.5 * paper
    darker = target < tight - delta
    inner = darker & own
    outer = darker & ~own & (target < print_level - 0.5 * delta)
    if overflow_only:
        if np.count_nonzero((target < near - delta) & (own | outer)) < _OVERFLOW_PIXELS:
            return False
    elif _ink_share(target, near, disc, paper, delta) < _CONTAMINATED:
        return False
    grow = np.ones((3, 3), np.uint8)
    inner_mask = cv2.dilate(inner.astype(np.uint8), grow, iterations=2) > 0
    outer_mask = (cv2.dilate(outer.astype(np.uint8), grow, iterations=2) > 0) & ~inner_mask
    if not (inner_mask.any() or outer_mask.any()):
        return False

    source = _patch(image, exemplar, reach, *offset).astype(np.float64)
    patch = _patch(image, bubble, reach).astype(np.float64)
    # Match the paper: the two backgrounds' ratio, where the target is clean
    # and outside both rings.
    around = ~inner_mask & ~outer_mask & ~_disc(reach, 1.05 * bubble.radius)
    gain: NDArray[np.float64] | float = 1.0
    if np.count_nonzero(around) >= 15:
        gain = np.median(patch[around], axis=0) / np.maximum(
            np.median(source[around], axis=0), 1.0
        )
    out = _patch(result, bubble, reach)
    out[inner_mask] = np.clip(source[inner_mask] * gain, 0, 255).astype(np.uint8)
    if outer_mask.any():
        # Beyond the ring there is no copy to take from; fill from around,
        # along whichever axis carries a printed line on through the gap.
        # The bubble's own ring is curved, so it is never an end to continue
        # from - two points of it above and below a gap would "agree" and
        # paint a bar of ring colour down the paper beside it.
        ring = _disc(reach, _OWN_PRINT * bubble.radius + 2.0)
        _fill_along_lines(out, outer_mask, outer_mask | ring)
    return True


def _fill_along_lines(
    patch: NDArray[np.uint8], mask: NDArray[np.bool_], blocked: NDArray[np.bool_]
) -> None:
    """Fill ``mask`` in place from the nearest pixels not ``blocked``, line-aware.

    Each masked pixel is interpolated between the nearest usable pixels above
    and below it, or to its left and right - whichever pair agrees better. A
    frame or table line the ink crossed has the same colour on both sides of
    the gap along its own direction, so it is continued; across bare paper
    both pairs agree and give paper. A general inpainting fills a long gap in
    a line from the paper around it and leaves the line broken.
    """
    values = patch.astype(np.float64)
    if values.ndim == 2:
        values = values[..., np.newaxis]
    filled = values.copy()
    rows, columns = mask.shape
    for y, x in np.argwhere(mask):
        best: tuple[float, NDArray[np.float64]] | None = None
        for vertical in (True, False):
            line = blocked[:, x] if vertical else blocked[y, :]
            samples = values[:, x] if vertical else values[y, :]
            position, limit = (y, rows) if vertical else (x, columns)
            before = position - 1
            while before >= 0 and line[before]:
                before -= 1
            after = position + 1
            while after < limit and line[after]:
                after += 1
            # Past the ink's soft edge, which is neither paper nor print and
            # would smear down the gap: a little further out, where still usable.
            for _ in range(_HALO):
                if before - 1 >= 0 and not line[before - 1]:
                    before -= 1
                if after + 1 < limit and not line[after + 1]:
                    after += 1
            ends = []
            if before >= 0:
                ends.append((before, samples[before]))
            if after < limit:
                ends.append((after, samples[after]))
            if not ends:
                continue
            if len(ends) == 1:
                estimate, disagreement = ends[0][1], float(np.inf)
            else:
                (a, va), (b, vb) = ends
                weight = (position - a) / (b - a)
                estimate = va + weight * (vb - va)
                disagreement = float(np.abs(va - vb).sum())
            if best is None or disagreement < best[0]:
                best = (disagreement, estimate)
        if best is not None:
            filled[y, x] = best[1]
    result = np.clip(filled, 0, 255).astype(np.uint8)
    patch[...] = result if patch.ndim == 3 else result[..., 0]


def _ink_share(
    target: NDArray[np.int16],
    reference: NDArray[np.int16],
    disc: NDArray[np.bool_],
    paper: float,
    delta: float,
) -> float:
    """Share of the disc's bare paper - by ``reference`` - that ``target`` inks."""
    bare = disc & (reference > _BARE_RATIO * paper)
    if not bare.any():
        return 0.0
    return float(np.mean((target < reference - delta)[bare]))


def _inpaint(
    image: NDArray[np.uint8],
    grey: NDArray[np.uint8],
    result: NDArray[np.uint8],
    bubble: _Bubble,
) -> bool:
    """With no clean copy of the label: fill an unmistakable fill from around it."""
    square = _square(bubble.half)
    patch = _patch(grey, bubble, square)
    disc = _disc(square, 0.85 * bubble.radius)
    fill = patch < _FILL_RATIO * _paper(patch)
    if np.mean(fill[disc]) < 0.5:
        # Without an exemplar a light mark cannot be told from printing; only
        # an unmistakable fill is removed.
        return False
    ink = (fill & _disc(square, 1.2 * bubble.radius)).astype(np.uint8) * 255
    mask = cv2.dilate(ink, np.ones((3, 3), np.uint8), iterations=2)
    window = _patch(result, bubble, square)
    window[...] = cv2.inpaint(np.ascontiguousarray(_patch(image, bubble, square)),
                              mask, 3, cv2.INPAINT_TELEA)
    return True


__all__ = ["BubbleCleanup", "clean_reference_bubbles"]
