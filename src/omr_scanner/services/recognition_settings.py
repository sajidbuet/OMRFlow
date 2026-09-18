"""What the recognition engine may do, in one validated object.

Purpose:
    Give a caller one place to say "read sheets like *this*" - how bubbles are
    sampled, whether a preview is produced, whether the per-bubble evidence is
    kept, where diagnostics go - instead of threading half a dozen keyword
    arguments through every layer between the GUI and the pixels.

Responsibilities:
    * :class:`RecognitionOptions` - engine-level options, validated on
      construction, immutable, and picklable so a worker process can receive one.
    * :class:`DiagnosticsOptions` - the opt-in debug output described in
      ``docs/scan_workflow.md``.

What does NOT belong here, and why:
    * **Recognition thresholds.** How dark a bubble must be, how far ahead of
      its runner-up, what counts as blank - all of those live in
      :class:`~omr_scanner.domain.template.RecognitionSettings`, inside the
      ``.omrt`` document, because they are properties of a *sheet design* and
      its print quality. Duplicating them here would create two sources of truth
      and let a machine-level preference silently change a recognised answer.
    * **Worker counts.** Those are a machine property and live in
      :mod:`omr_scanner.config.processing`.
    * Anything with no effect. Every field below changes what the engine
      actually does; none of them exists merely to be configurable.

The division in one line:
    the template says *what a mark is*; these options say *how much work the
    engine does and what it hands back*.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from omr_scanner.imaging.metrics import BubbleMetricsConfig

DEFAULT_PREVIEW_MAX_DIMENSION = 1400
"""Longest side of the preview image handed to a caller, in pixels.

A rectified A4 page at 300 dpi is about 8 MB as grayscale bytes; a batch of a
hundred would be most of a gigabyte held for no reason, because only the
selected scan is ever on screen. Downscaling the *preview* costs nothing in
accuracy - every measurement was already taken at full resolution."""

DEFAULT_QUALITY_SAMPLE_MAX_DIMENSION = 1000
"""Longest side of the image the scan-quality statistics are computed on.

Brightness, contrast and sharpness are diagnostic summaries, not measurements
that decide anything, so computing them on a bounded downsample keeps their cost
flat across scanner resolutions instead of growing with the page."""


@dataclass(frozen=True, slots=True)
class DiagnosticsOptions:
    """Where and whether the engine writes its intermediate images.

    Diagnostics are off by default and must stay that way: they multiply the
    bytes a batch writes by roughly the number of stages, and a normal run has
    no use for them.

    Attributes:
        enabled: Write anything at all.
        directory: Root folder for the output. Each scan gets its own
            sub-folder, named after the scan, so a batch of hundreds does not
            become one flat directory of thousands of files.
        stages: Which stages to write. Empty means "every stage this engine
            produces"; naming a subset keeps a long run's output small.
        overlay: Also render the recognition overlay - the picture that shows
            what was decided, which is the single most useful artefact when a
            sheet reads wrongly.
        failures_only: Write nothing for a scan that came back clean. The usual
            setting for a large batch: the interesting sheets are the ones that
            failed or need review.
    """

    enabled: bool = False
    directory: Path | None = None
    stages: frozenset[str] = frozenset()
    overlay: bool = True
    failures_only: bool = False

    def __post_init__(self) -> None:
        """Reject a configuration that could not produce anything."""
        if self.enabled and self.directory is None:
            raise ValueError("Diagnostics are enabled but no output directory was given")

    def wants(self, stage: str) -> bool:
        """Whether ``stage`` should be written."""
        if not self.enabled:
            return False
        return not self.stages or stage in self.stages

    def for_scan(self, name: str) -> Path | None:
        """Return the folder this scan's diagnostics belong in, or ``None``."""
        if not self.enabled or self.directory is None:
            return None
        return self.directory / name


@dataclass(frozen=True, slots=True)
class RecognitionOptions:
    """Engine-level options for reading one sheet.

    Attributes:
        metrics: Bubble sampling tuning - where inside the printed ring the
            sample is taken, how the local paper level is estimated. Geometry
            and signal processing, not thresholds.
        with_preview: Produce a display image of the rectified page. Batch runs
            turn this off: only the sheet being looked at is ever shown.
        preview_max_dimension: Longest side of that preview, or ``None`` to keep
            the rectified page at full size.
        keep_bubble_measurements: Keep the per-bubble records - position, fill
            ratio, darkness, contrast, the threshold each was compared against
            - on the result. On by default, because those numbers are what make
            a future recalibration possible without re-reading every image,
            what an overlay draws, and what a reviewer asking "why was this
            flagged?" needs.

            Turning it off omits the records entirely rather than blanking
            them, because they *are* the weight: a hundred-question sheet
            carries five hundred of them against a hundred answers. A caller
            that keeps results for ten thousand sheets and only needs the
            values - the Scan page, building a CSV - saves roughly an order of
            magnitude of memory by declining them. No decision changes either
            way.
        keep_quality_metrics: Measure the scan's brightness, contrast and
            sharpness. Cheap, diagnostic, and never consulted by a decision.
        diagnostics: Debug-image output; off unless asked for.

    Immutability:
        Frozen, and every field is itself immutable or a plain value, so one
        options object can be shared by every worker in a pool without any
        chance of one run's settings drifting into another's.
    """

    metrics: BubbleMetricsConfig = field(default_factory=BubbleMetricsConfig)
    with_preview: bool = True
    preview_max_dimension: int | None = DEFAULT_PREVIEW_MAX_DIMENSION
    keep_bubble_measurements: bool = True
    keep_quality_metrics: bool = True
    diagnostics: DiagnosticsOptions = field(default_factory=DiagnosticsOptions)

    def __post_init__(self) -> None:
        """Reject values that would fail later, in the middle of a batch."""
        if self.preview_max_dimension is not None and self.preview_max_dimension < 1:
            raise ValueError(
                f"preview_max_dimension must be at least 1 pixel, got "
                f"{self.preview_max_dimension}"
            )

    def with_preview_disabled(self) -> RecognitionOptions:
        """Return a copy that produces no preview - what a batch run uses."""
        return replace(self, with_preview=False)

    def with_diagnostics(self, diagnostics: DiagnosticsOptions) -> RecognitionOptions:
        """Return a copy writing debug output as ``diagnostics`` describes."""
        return replace(self, diagnostics=diagnostics)


DEFAULT_OPTIONS = RecognitionOptions()
"""The options a caller gets when it expresses no preference."""


__all__ = [
    "DEFAULT_OPTIONS",
    "DEFAULT_PREVIEW_MAX_DIMENSION",
    "DEFAULT_QUALITY_SAMPLE_MAX_DIMENSION",
    "DiagnosticsOptions",
    "RecognitionOptions",
]
