"""Loading one sheet for conflict review, off the GUI thread.

Purpose:
    Decode the original scan and re-read it with full per-bubble evidence, so
    the review workspace can show what the recogniser saw without the window
    freezing while it does.

Responsibilities:
    * :class:`SheetBundle` - the original image, the rectified page and the
      evidence, as one value.
    * :class:`SheetWorker` - produces one, in the background.

What does NOT belong here:
    * Widgets. Nothing in this module touches one; the page decides what to
      draw when the signal arrives on the main thread.
    * Conflict logic. This worker knows nothing about conflicts - it loads a
      sheet, and the page picks the region out of it.

Why the sheet is read again rather than stored:
    A batch deliberately discards per-bubble evidence
    (``keep_bubble_measurements`` is off for a batch, because five hundred
    records per sheet is most of a gigabyte over ten thousand sheets) and
    deliberately discards previews for the same reason. Review needs both, for
    one sheet at a time. Recognition is deterministic - same image, same
    template, same answer - so re-reading the one sheet being looked at
    reproduces exactly the evidence behind the stored result, at the cost of a
    few hundred milliseconds in a background thread.

    This is the same pattern the Scan page's ``PreviewWorker`` has used since
    Phase 3, for the same reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from omr_scanner.services import (
    DecodedImage,
    RecognitionOptions,
    ScanResult,
    decode_image_file,
    recognise_scan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SheetBundle:
    """Everything the review workspace needs about one sheet.

    Attributes:
        path: The scan that was loaded.
        original: The file as it arrived, decoded for display only. **Never
            written back** - this is a copy in memory, and Phase 5's
            "originals are byte-for-byte unchanged" rule applies unchanged to
            Phase 6.
        result: A fresh recognition of it, with a preview and per-bubble
            evidence. ``None`` when the sheet could not be read at all, which
            is itself reviewable.
        error: Why it could not be read, or ``""``.
    """

    path: Path
    original: DecodedImage | None = None
    result: ScanResult | None = None
    error: str = ""

    @property
    def has_evidence(self) -> bool:
        """Whether per-bubble measurements are available for this sheet."""
        return self.result is not None and bool(self.result.bubbles)

    @property
    def registered(self) -> bool:
        """Whether the sheet rectified, and therefore has canonical geometry."""
        return self.result is not None and self.result.preview is not None


class SheetWorker(QThread):
    """Loads one sheet for review in the background.

    Signals:
        ready: :class:`SheetBundle` once the sheet has been loaded and read.
            Emitted even when the read failed, carrying the reason - a sheet
            that cannot be decoded is exactly the sort of thing a reviewer is
            being asked to look at.

    Args:
        path: The scan to load.
        template: The template to read it with.
        parent: Optional Qt parent.
    """

    ready = Signal(object)

    def __init__(
        self, path: Path, template: OmrTemplate, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._template = template

    @property
    def path(self) -> Path:
        """The sheet this worker is loading."""
        return self._path

    def run(self) -> None:
        """Decode and re-read the sheet. Runs on the worker thread."""
        original: DecodedImage | None = None
        try:
            original = decode_image_file(self._path)
        except Exception as exc:
            # A sheet whose file has since been moved or corrupted is a
            # legitimate review subject, not a crash. The rectified view will
            # be empty and the reason is shown instead.
            _LOGGER.info("Original scan %s could not be decoded: %s", self._path.name, exc)
            self.ready.emit(SheetBundle(path=self._path, error=str(exc)))
            return

        try:
            result = recognise_scan(
                self._path,
                self._template,
                options=RecognitionOptions(
                    with_preview=True,
                    keep_bubble_measurements=True,
                    keep_quality_metrics=True,
                ),
            )
        except Exception as exc:
            _LOGGER.exception("Sheet %s could not be re-read for review", self._path.name)
            self.ready.emit(
                SheetBundle(path=self._path, original=original, error=str(exc))
            )
            return

        self.ready.emit(SheetBundle(path=self._path, original=original, result=result))


__all__ = ["SheetBundle", "SheetWorker"]
