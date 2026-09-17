"""Turning the measurements of one response group into a decision.

Purpose:
    Answer, for one set of mutually exclusive bubbles, the only question the
    pixels cannot: which of them did the candidate mean to mark - and, just as
    importantly, when that cannot be told.

Responsibilities:
    * :func:`decide_group` - the single decision rule, applied identically to a
      digit column, a set-code column and a question's options.

What does NOT belong here:
    * Pixel access. The readings arrive already measured by
      :mod:`omr_scanner.imaging.metrics`.
    * Field assembly ("these seven columns spell a roll number"), which is
      :mod:`omr_scanner.recognition.fields`.
    * Thresholds as module constants. Every number used here comes from the
      template's :class:`~omr_scanner.domain.template.RecognitionSettings`.

The rule, in words:
    A bubble counts as *marked* when its fill ratio reaches
    ``fill_ratio_threshold``. From there:

    * two or more marked  -> ``MULTIPLE``; every mark is kept.
    * exactly one marked  -> ``RESOLVED`` if it leads the next darkest bubble by
      at least ``ambiguity_margin``, otherwise ``UNCERTAIN`` - the mark is still
      reported, but flagged, because two similar readings are how a half-erased
      answer looks.
    * none marked, darkest below ``blank_ratio_threshold`` -> ``BLANK``.
    * none marked, darkest between the two thresholds -> ``UNCERTAIN``: there is
      something there, but not enough to call it an answer. No value is
      produced; the darkest bubble is recorded as
      :attr:`~omr_scanner.recognition.models.GroupDecision.leading_index` so a
      reviewer can see what it nearly was.

Why the margin matters more than the darkness:
    Absolute darkness varies with pencil grade, pressure, paper and scanner
    exposure, and a threshold tuned on one batch is wrong for the next. The
    *difference* between the darkest bubble and the next one in the same group
    is measured under identical conditions - same pencil, same paper, same few
    square centimetres of the same scan - so it survives all of that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.recognition.models import BubbleReading, GroupDecision, MarkStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import RecognitionSettings
    from omr_scanner.imaging.metrics import BubbleMeasurement


def reading_from_measurement(
    measurement: BubbleMeasurement | None, label: str
) -> BubbleReading:
    """Pair one measurement with the symbol its position stands for.

    Args:
        measurement: The bubble's measurement, or ``None`` when the bubble was
            never measured - a zone whose grid extends past the page, or a
            caller that supplied an incomplete grid.
        label: The symbol this bubble represents.

    Returns:
        The reading. A missing measurement becomes an *unusable* reading rather
        than a zero-fill one, so the group is reported as unreadable instead of
        confidently blank.
    """
    if measurement is None:
        return BubbleReading(
            label=label, fill_ratio=0.0, mean_darkness=0.0, contrast=0.0, usable=False
        )
    return BubbleReading(
        label=label,
        fill_ratio=measurement.fill_ratio,
        mean_darkness=measurement.mean_darkness,
        contrast=measurement.contrast,
        usable=measurement.usable,
    )


def readings_from_measurements(
    measurements: Sequence[BubbleMeasurement], labels: Sequence[str]
) -> tuple[BubbleReading, ...]:
    """Pair a group's measurements with the symbols its positions stand for.

    Args:
        measurements: One measurement per bubble, in printed order.
        labels: The symbol each bubble represents, in the same order.

    Returns:
        The readings, ready for :func:`decide_group`.

    Raises:
        ValueError: The two sequences have different lengths, which would
            silently mis-label every answer after the mismatch.
    """
    if len(measurements) != len(labels):
        raise ValueError(
            f"Got {len(measurements)} measurements for {len(labels)} labels; "
            "every bubble in a group must have exactly one symbol"
        )
    return tuple(
        reading_from_measurement(measurement, label)
        for measurement, label in zip(measurements, labels, strict=True)
    )


def decide_group(
    readings: Sequence[BubbleReading], *, settings: RecognitionSettings
) -> GroupDecision:
    """Decide what one group of mutually exclusive bubbles contains.

    Args:
        readings: The group's bubbles, in printed order. The order is preserved
            in the result, so a multiply-marked group reads ``"B-D"`` rather
            than ``"D-B"``.
        settings: The thresholds that apply to this zone, from the template.

    Returns:
        The decision, carrying the evidence behind it.

    Raises:
        ValueError: ``readings`` is empty - a response group with no bubbles is
            a malformed template, not a blank answer.
    """
    if not readings:
        raise ValueError("A response group must contain at least one bubble")

    usable = [index for index, reading in enumerate(readings) if reading.usable]
    if not usable:
        return GroupDecision(
            readings=tuple(readings),
            selected=(),
            status=MarkStatus.UNREADABLE,
            leading_index=None,
            top_fill=0.0,
            runner_up_fill=0.0,
            margin=0.0,
            confidence=0.0,
            needs_review=True,
        )

    ranked = sorted(usable, key=lambda index: readings[index].fill_ratio, reverse=True)
    leading_index = ranked[0]
    top_fill = readings[leading_index].fill_ratio
    runner_up_fill = readings[ranked[1]].fill_ratio if len(ranked) > 1 else 0.0
    margin = top_fill - runner_up_fill

    # Selection uses the template's own fill threshold, and keeps printed order
    # so that a double mark reads left-to-right as it appears on the paper.
    selected = tuple(
        index
        for index in sorted(usable)
        if readings[index].fill_ratio >= settings.fill_ratio_threshold
    )

    if len(selected) >= 2:
        status = MarkStatus.MULTIPLE
        confidence = 0.0
    elif len(selected) == 1:
        if margin >= settings.ambiguity_margin:
            status = MarkStatus.RESOLVED
            # 1.0 once the separation reaches what the template demands; below
            # that it falls linearly, so the number means "how much of the
            # required separation was actually achieved" and nothing else.
            confidence = _ratio(margin, settings.ambiguity_margin)
        else:
            status = MarkStatus.UNCERTAIN
            confidence = 0.0
    elif top_fill < settings.blank_ratio_threshold:
        status = MarkStatus.BLANK
        # 1.0 when the darkest bubble is completely clean; falls to 0.0 as it
        # approaches the "certainly empty" ceiling.
        confidence = _ratio(
            settings.blank_ratio_threshold - top_fill, settings.blank_ratio_threshold
        )
    else:
        # Something is there, but not enough of it. Deliberately no value.
        status = MarkStatus.UNCERTAIN
        confidence = 0.0

    decided = status in (MarkStatus.RESOLVED, MarkStatus.BLANK)
    needs_review = not decided or confidence < settings.min_confidence

    return GroupDecision(
        readings=tuple(readings),
        selected=selected,
        status=status,
        leading_index=leading_index,
        top_fill=top_fill,
        runner_up_fill=runner_up_fill,
        margin=margin,
        confidence=confidence,
        needs_review=needs_review,
    )


def _ratio(achieved: float, required: float) -> float:
    """Return ``achieved / required`` clamped to ``[0, 1]``.

    ``required`` of zero means the template asks for no separation at all, in
    which case any outcome satisfies it completely.
    """
    if required <= 0.0:
        return 1.0
    return max(0.0, min(1.0, achieved / required))


__all__ = ["decide_group", "reading_from_measurement", "readings_from_measurements"]
