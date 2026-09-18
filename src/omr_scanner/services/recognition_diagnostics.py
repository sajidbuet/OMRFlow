"""Seeing what recognition did, without a GUI.

Purpose:
    Turn a :class:`~omr_scanner.services.recognition_models.ScanResult` back
    into pictures - the rectified page, the zones the template projected onto
    it, every bubble that was measured and what was decided about it - so that
    "why did this sheet read wrongly?" can be answered from a terminal, a
    server, a test, or a bug report containing a folder of PNGs.

Responsibilities:
    * :func:`render_overlay` - one annotated image of a finished result.
    * :func:`write_diagnostics` - the staged debug output for one scan.

What does NOT belong here:
    * Qt. Everything is drawn with OpenCV into a NumPy array, which is what
      makes this runnable headlessly and reusable by a future review screen -
      the GUI can display the same image rather than reimplementing the drawing.
    * Any influence on recognition. Nothing in this module is consulted by a
      decision; it reads a finished result and draws it. Generating diagnostics
      must never change what a sheet says, and a test asserts exactly that.

Why it is off by default:
    A staged dump is three or four full-page images per sheet. On a batch of a
    thousand that is gigabytes, written for nobody. Diagnostics are opt-in, they
    write into their own directory, and ``failures_only`` narrows them further
    to the sheets a human would actually open.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, cast

import cv2
import numpy as np

from omr_scanner.recognition.models import MarkStatus
from omr_scanner.services.recognition_models import RecognitionOutcome, ScanResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from numpy.typing import NDArray

    from omr_scanner.services.recognition_settings import DiagnosticsOptions

_LOGGER = logging.getLogger(__name__)

STAGE_ORIGINAL = "00_original"
STAGE_REGISTERED = "01_registered"
STAGE_OVERLAY = "02_overlay"
STAGE_MEASUREMENTS = "03_measurements"
RESULT_DOCUMENT = "result.json"
"""The stages this engine can produce. Named with a numeric prefix so a file
listing reads in pipeline order rather than alphabetically."""

ALL_STAGES: tuple[str, ...] = (
    STAGE_ORIGINAL,
    STAGE_REGISTERED,
    STAGE_OVERLAY,
    STAGE_MEASUREMENTS,
    RESULT_DOCUMENT,
)

# Colours are BGR, because that is what OpenCV draws in. Chosen to stay
# distinguishable when printed in greyscale as well as on screen: the selected
# mark is the darkest, an attention state is mid-tone, and an unremarkable
# bubble is light.
_COLOR_SELECTED = (32, 160, 32)
_COLOR_MULTIPLE = (48, 48, 220)
_COLOR_UNCERTAIN = (40, 170, 240)
_COLOR_LEADING = (200, 140, 40)
_COLOR_EMPTY = (190, 190, 190)
_COLOR_ZONE = (140, 140, 140)
_COLOR_MARKER = (200, 60, 200)
_COLOR_TEXT = (40, 40, 40)

_ZONE_THICKNESS = 2
_BUBBLE_THICKNESS = 2
_LABEL_SCALE = 0.45


def render_overlay(
    page: NDArray[np.uint8],
    result: ScanResult,
    *,
    show_empty: bool = False,
    annotate_measurements: bool = False,
) -> NDArray[np.uint8]:
    """Draw one result's decisions over the rectified page.

    Args:
        page: The rectified page, in canonical pixels - the same frame every
            coordinate on ``result`` is expressed in, which is why no mapping
            is needed here and why a mismatch would be immediately visible.
        result: The finished result to illustrate.
        show_empty: Also outline bubbles that were measured as empty. Off by
            default because a hundred questions is four hundred rings; on, it
            answers the other question - "did the grid land where the template
            said?".
        annotate_measurements: Print each drawn bubble's fill ratio beside it.

    Returns:
        A new BGR image. The input is never modified: a diagnostic that altered
        the page it was documenting would be worse than no diagnostic.
    """
    canvas = _as_colour(page)

    for zone in result.zones:
        top_left = (round(zone.x), round(zone.y))
        bottom_right = (round(zone.x + zone.width), round(zone.y + zone.height))
        cv2.rectangle(canvas, top_left, bottom_right, _zone_colour(zone.color), _ZONE_THICKNESS)
        cv2.putText(
            canvas,
            zone.label,
            (top_left[0], max(top_left[1] - 6, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            _LABEL_SCALE,
            _COLOR_TEXT,
            1,
            cv2.LINE_AA,
        )

    for bubble in result.bubbles:
        colour, thickness = _bubble_style(bubble.selected, bubble.leading, bubble.group_status)
        if colour is None and not show_empty:
            continue
        centre = (round(bubble.x), round(bubble.y))
        axes = (max(round(bubble.width / 2), 1), max(round(bubble.height / 2), 1))
        cv2.ellipse(canvas, centre, axes, 0, 0, 360, colour or _COLOR_EMPTY, thickness)
        if annotate_measurements:
            cv2.putText(
                canvas,
                f"{bubble.fill_ratio:.2f}",
                (centre[0] + axes[0] + 2, centre[1] + 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                _LABEL_SCALE * 0.8,
                _COLOR_TEXT,
                1,
                cv2.LINE_AA,
            )

    _draw_header(canvas, result)
    return canvas


def _bubble_style(
    selected: bool, leading: bool, group_status: str
) -> tuple[tuple[int, int, int] | None, int]:
    """Return the colour and thickness one bubble should be drawn with.

    ``None`` means "nothing interesting happened here" - the caller decides
    whether to draw it at all, which is what keeps the default overlay readable
    on a hundred-question sheet.
    """
    if group_status == MarkStatus.MULTIPLE.value and selected:
        return _COLOR_MULTIPLE, _BUBBLE_THICKNESS + 1
    if selected:
        return _COLOR_SELECTED, _BUBBLE_THICKNESS + 1
    if group_status == MarkStatus.UNCERTAIN.value and leading:
        return _COLOR_UNCERTAIN, _BUBBLE_THICKNESS
    if leading:
        return _COLOR_LEADING, 1
    return None, 1


def _draw_header(canvas: NDArray[np.uint8], result: ScanResult) -> None:
    """Stamp the identifying facts onto the image itself.

    On the image rather than only in a side-car file, because a diagnostic
    picture is routinely pasted into a bug report on its own, and one that does
    not say which sheet, which engine and which template produced it starts an
    argument nobody can settle.
    """
    lines = [
        f"{result.source_path.name}  [{result.engine_name} {result.engine_version}]",
        f"template: {result.template_name or result.template_id or 'unknown'}",
        f"roll: {result.identifier_value or '-'}   set: {result.set_code_value or '-'}",
        f"status: {', '.join(result.status_codes) or '-'}",
    ]
    for index, line in enumerate(lines):
        cv2.putText(
            canvas,
            line,
            (12, 26 + index * 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            _COLOR_TEXT,
            1,
            cv2.LINE_AA,
        )


def _as_colour(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return a writable three-channel copy of ``image``."""
    if image.ndim == 2:
        return cast("NDArray[np.uint8]", cv2.cvtColor(image, cv2.COLOR_GRAY2BGR))
    return image.copy()


def _zone_colour(value: str) -> tuple[int, int, int]:
    """Convert a template's ``#RRGGBB`` display colour into an OpenCV BGR tuple."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return _COLOR_ZONE
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return _COLOR_ZONE
    return blue, green, red


def write_diagnostics(
    result: ScanResult,
    options: DiagnosticsOptions,
    *,
    original: NDArray[np.uint8] | None = None,
    registered: NDArray[np.uint8] | None = None,
) -> tuple[Path, ...]:
    """Write one scan's debug output, and return the files written.

    Args:
        result: The finished result.
        options: Where to write and what to include.
        original: The scan as loaded, before rectification.
        registered: The rectified page. Required for the overlay stages; when
            it is absent (a sheet that never registered) those stages are
            skipped rather than faked.

    Returns:
        The paths written, in stage order. Empty when diagnostics are disabled
        or this scan was filtered out by ``failures_only``.

    Never raises for an I/O problem: diagnostics are an aid, and a full disk
    must not turn a readable sheet into a failed one. A failure is logged and
    the recognition result stands.
    """
    folder = options.for_scan(result.source_path.stem)
    if folder is None:
        return ()
    if options.failures_only and _is_clean(result):
        return ()

    written: list[Path] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)

        if original is not None and options.wants(STAGE_ORIGINAL):
            written.append(_write_image(folder, STAGE_ORIGINAL, original))
        if registered is not None and options.wants(STAGE_REGISTERED):
            written.append(_write_image(folder, STAGE_REGISTERED, registered))
        if registered is not None and options.overlay and options.wants(STAGE_OVERLAY):
            written.append(
                _write_image(folder, STAGE_OVERLAY, render_overlay(registered, result))
            )
        if registered is not None and options.wants(STAGE_MEASUREMENTS):
            written.append(
                _write_image(
                    folder,
                    STAGE_MEASUREMENTS,
                    render_overlay(
                        registered, result, show_empty=True, annotate_measurements=True
                    ),
                )
            )
        if options.wants(RESULT_DOCUMENT):
            document = folder / RESULT_DOCUMENT
            document.write_text(
                json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            written.append(document)
    except (OSError, cv2.error) as exc:
        _LOGGER.warning(
            "Could not write diagnostics for %s into %s: %s", result.source_path.name, folder, exc
        )
        return tuple(written)

    _LOGGER.debug("Wrote %d diagnostic file(s) for %s", len(written), result.source_path.name)
    return tuple(written)


def _is_clean(result: ScanResult) -> bool:
    """Whether this scan is one a human would have no reason to look at."""
    return result.outcome is RecognitionOutcome.COMPLETE and not result.warnings


def _write_image(folder: Path, stage: str, image: NDArray[np.uint8]) -> Path:
    """Write one stage image as PNG and return its path."""
    destination = folder / f"{stage}.png"
    # `cv2.imwrite` cannot handle a non-ASCII path on Windows; encoding to a
    # buffer and writing it with pathlib works everywhere, and a diagnostics
    # folder inevitably ends up under somebody's accented user name.
    success, buffer = cv2.imencode(".png", image)
    if not success:  # pragma: no cover - PNG encoding of a valid array
        raise OSError(f"Could not encode the {stage} diagnostic image")
    destination.write_bytes(buffer.tobytes())
    return destination


__all__ = [
    "ALL_STAGES",
    "RESULT_DOCUMENT",
    "STAGE_MEASUREMENTS",
    "STAGE_ORIGINAL",
    "STAGE_OVERLAY",
    "STAGE_REGISTERED",
    "render_overlay",
    "write_diagnostics",
]
