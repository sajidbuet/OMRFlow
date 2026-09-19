"""Recognising one scanned sheet, end to end.

Purpose:
    Be the one door into Phase 3. Alignment (Phase 1), bubble measurement
    (:mod:`omr_scanner.imaging.metrics`) and interpretation
    (:mod:`omr_scanner.recognition`) each know nothing about the others; this
    module is where they meet, and it is the only thing the rest of the
    application - the Scan page, the batch processor, a future review or scoring
    phase - is permitted to know about.

Responsibilities:
    * :class:`RecognitionEngine` - the stable entry point: "read this image with
      this template". Holds its options, so a batch configures once and reads
      many.
    * :func:`recognise_scan` - the same thing as one function call, kept because
      most callers read exactly one sheet and because it is the signature the
      rest of the repository already uses.
    * Translate every result into **plain data** - strings, floats, bytes - so
      that the Scan page can display it without importing ``numpy``,
      ``omr_scanner.imaging`` or ``omr_scanner.recognition``, all of which are
      forbidden to the GUI layer (``docs/ARCHITECTURE.md``).
    * Never raise for an unreadable or unalignable sheet: a batch has to
      continue, so a failure becomes a *result* with a status and a message.

What does NOT belong here:
    * Any pixel algorithm or decision threshold of its own. Both come from the
      layers below, and every threshold ultimately from the template.
    * The result vocabulary, which is
      :mod:`omr_scanner.services.recognition_models`, so that a consumer can
      depend on the shape of a result without importing the engine that fills
      it - which is what lets Phase 4 be written and tested against stored
      results before Recognition Engine v2 exists.
    * File naming, copying or CSV writing; those are
      :mod:`omr_scanner.services.filename_manager`,
      :mod:`omr_scanner.services.batch_processor` and
      :mod:`omr_scanner.services.scan_export`.
    * Qt of any kind.

Replaceability, which is the point of the boundary:
    A future engine may threshold differently, measure differently or register
    differently. As long as it returns a
    :class:`~omr_scanner.services.recognition_models.ScanResult` and stamps its
    own :attr:`engine_version`, nothing above this line has to change - and a
    benchmark can put the two versions side by side on one dataset.

Coordinate systems, in one place:
    Three frames are in play and confusing them is the classic OMR bug.

    * **Source pixels** - the scanner's own image, whatever size it happens to
      be. Only :attr:`ScanResult.markers` is expressed here, because that is
      where the registration marks were physically found.
    * **Normalised template coordinates** - ``0..1`` on the canonical page, what
      the ``.omrt`` document stores.
    * **Canonical pixels** - the rectified page,
      ``template.page.canonical_width_px`` by ``canonical_height_px``. Every
      overlay and every measurement in a :class:`ScanResult` is in this frame,
      which is also the frame :attr:`ScanResult.preview` is an image of, so the
      GUI can draw overlays directly over the preview with no further mapping.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, cast

import numpy as np

from omr_scanner.domain.template import IgnoredFieldDefinition
from omr_scanner.errors import ImagingError, OMRScannerError
from omr_scanner.imaging.alignment import align_sheet
from omr_scanner.imaging.geometry import apply_transform
from omr_scanner.imaging.metrics import (
    BubbleMeasurement,
    BubbleMetricsConfig,
    estimate_ink_level,
    ink_threshold,
    measure_bubbles,
)
from omr_scanner.imaging.models import Point
from omr_scanner.recognition.fields import recognise_template, zone_groups
from omr_scanner.recognition.models import FieldStatus, MarkStatus
from omr_scanner.services.alignment_service import alignment_config_from_template, load_scan_image
from omr_scanner.services.marker_detection_service import DecodedImage
from omr_scanner.services.recognition_models import (
    ENGINE_NAME,
    ENGINE_VERSION,
    RESULT_SCHEMA_VERSION,
    AnswerView,
    BubbleView,
    CharacterView,
    FieldView,
    MarkerView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanQuality,
    ScanResult,
    StageTimings,
    StatusCode,
    ZoneView,
    derive_status_codes,
    utc_timestamp,
)
from omr_scanner.services.recognition_settings import (
    DEFAULT_PREVIEW_MAX_DIMENSION,
    DEFAULT_QUALITY_SAMPLE_MAX_DIMENSION,
    DiagnosticsOptions,
    RecognitionOptions,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from numpy.typing import NDArray

    from omr_scanner.domain.template import OmrTemplate, Zone
    from omr_scanner.imaging.models import AlignmentResult, RegistrationMarkerDetection
    from omr_scanner.recognition.models import SheetRecognition

_LOGGER = logging.getLogger(__name__)


class RecognitionEngine:
    """Reads sheets. The stable interface every other layer calls.

    Callers construct one, configure it once, and hand it images and templates.
    They are not expected to know - and must not depend on - how a bubble is
    sampled, how the page is rectified, or where a threshold comes from.

    Args:
        options: Engine options; the defaults read a sheet with a preview and
            keep the per-bubble evidence.

    Attributes:
        options: The options this engine was built with; immutable.

    Example:
        >>> engine = RecognitionEngine()                       # doctest: +SKIP
        >>> result = engine.process(Path("scan.png"), template)  # doctest: +SKIP
        >>> result.identifier_value                              # doctest: +SKIP
        '2103123'

    Thread and process safety:
        An engine holds no mutable state between calls, so one instance may be
        shared by several threads and pickled into a worker process. That is not
        an accident: it is what makes the multicore batch path (see
        :mod:`omr_scanner.services.parallel_batch`) safe to write.
    """

    name = ENGINE_NAME
    version = ENGINE_VERSION
    schema_version = RESULT_SCHEMA_VERSION

    def __init__(self, options: RecognitionOptions | None = None) -> None:
        self.options = options if options is not None else RecognitionOptions()

    def process(self, image_path: Path, template: OmrTemplate) -> ScanResult:
        """Read one scanned sheet.

        Args:
            image_path: Image file to read.
            template: The template describing the sheet.

        Returns:
            The result. Never raises for a bad sheet: a file that cannot be
            read, a page that cannot be registered and an unexpected internal
            failure all come back as a :class:`ScanResult` carrying an outcome,
            a status code and a message, because a batch must survive any one of
            its files.
        """
        return _recognise(image_path, template, self.options)

    def open_session(self, image_path: Path, template: OmrTemplate) -> CalibrationSession:
        """Register and measure one sheet, ready for repeated re-decision.

        For the calibration workflow (Phase 4): load the sheet once, and let
        the caller re-apply :attr:`~omr_scanner.domain.template.OmrTemplate.recognition`
        as many times as it likes - one call per slider movement - without
        registering or measuring again. See :class:`CalibrationSession`.

        Args:
            image_path: Image file to read.
            template: The template to register and measure against. Every
                later :meth:`CalibrationSession.recompute` must be given a
                template with the *same geometry* as this one; only its
                recognition settings may differ.

        Returns:
            The session. Registration failures are captured, not raised: a
            session for a sheet that could not be registered still opens, and
            simply reports that failure from every ``recompute`` call.
        """
        try:
            measured = _measure_sheet(image_path, template, self.options)
        except _SheetMeasurementError as early:
            return CalibrationSession(None, early.result, self.options)
        return CalibrationSession(measured, None, self.options)

    def describe(self) -> str:
        """One line identifying this engine, for a log or a report header."""
        return f"{self.name} {self.version} (result schema {self.schema_version})"


def recognise_scan(
    path: Path,
    template: OmrTemplate,
    *,
    options: RecognitionOptions | None = None,
    metrics_config: BubbleMetricsConfig | None = None,
    with_preview: bool = True,
    preview_max_dimension: int | None = DEFAULT_PREVIEW_MAX_DIMENSION,
    diagnostics: DiagnosticsOptions | None = None,
) -> ScanResult:
    """Read one scanned sheet with one template.

    The function form of :meth:`RecognitionEngine.process`, kept because most
    callers read a single sheet and because this is the signature the rest of
    the repository already uses.

    Args:
        path: Image file to read.
        template: The template describing the sheet.
        options: Engine options. When given, it supplies every setting and the
            individual keyword arguments below are ignored except where they
            were explicitly passed.
        metrics_config: Bubble sampling tuning; defaults apply when omitted.
        with_preview: Produce a display image of the rectified page. Batch
            processing turns this off - only the selected scan is ever shown,
            and a hundred full-page previews is a gigabyte held for nothing.
        preview_max_dimension: Longest side of that preview.
        diagnostics: Debug-image output; off unless asked for.

    Returns:
        The result; see :meth:`RecognitionEngine.process`.
    """
    resolved = _resolve_options(
        options,
        metrics_config=metrics_config,
        with_preview=with_preview,
        preview_max_dimension=preview_max_dimension,
        diagnostics=diagnostics,
    )
    return _recognise(path, template, resolved)


def _resolve_options(
    options: RecognitionOptions | None,
    *,
    metrics_config: BubbleMetricsConfig | None,
    with_preview: bool,
    preview_max_dimension: int | None,
    diagnostics: DiagnosticsOptions | None,
) -> RecognitionOptions:
    """Fold the legacy keyword arguments into one options object.

    Both spellings exist on purpose: ``options=`` is the one a new caller should
    use, and the individual keywords are what the existing callers in this
    repository (and any test written against them) already pass. Rather than
    deprecating them loudly and breaking working code, they are folded in here,
    in one place, where the precedence is visible: an explicit ``options``
    wins, and the keywords fill in what it did not say.
    """
    if options is not None:
        return options
    return RecognitionOptions(
        metrics=metrics_config if metrics_config is not None else BubbleMetricsConfig(),
        with_preview=with_preview,
        preview_max_dimension=preview_max_dimension,
        diagnostics=diagnostics if diagnostics is not None else DiagnosticsOptions(),
    )


# ----------------------------------------------------------------------
# The pipeline
# ----------------------------------------------------------------------
def _recognise(
    path: Path, template: OmrTemplate, options: RecognitionOptions
) -> ScanResult:
    """Load, align, measure, interpret and report one sheet.

    Split into named stages that each do one thing and hand the next one plain
    data, because that is what makes the boundary in the module docstring real:
    replacing the measurement stage, or the decision stage, means replacing one
    function here rather than unpicking a single long one.

    The split runs one level deeper than that sentence used to describe: load,
    align and measure happen in :func:`_measure_sheet`, and decide, build and
    present happen in :func:`_decide_and_build`. That is not cosmetic - it is
    the seam :class:`CalibrationSession` holds onto, because re-deciding a
    cached measurement under a different threshold must not repeat the
    registration that produced it.
    """
    try:
        measured = _measure_sheet(path, template, options)
    except _SheetMeasurementError as early:
        return early.result
    return _decide_and_build(measured, template, options)


@dataclass(frozen=True, slots=True)
class _MeasuredSheet:
    """Everything a recognition-settings change is not allowed to repeat.

    Attributes:
        source_path: The image that was read.
        image: The original scan, as loaded (grayscale).
        alignment: The Phase 1 registration result.
        page: The rectified page, exactly ``canonical_width`` x
            ``canonical_height``.
        page_ink: The page's estimated solid-ink grey level.
        measurements: Every zone's bubble measurements, keyed by ``(row,
            column)`` - the quantity :func:`~omr_scanner.recognition.fields.recognise_template`
            turns into values, and the only thing a threshold change may act
            on again.
        canonical_width: Canonical page width in pixels.
        canonical_height: Canonical page height in pixels.
        source_width: Source scan width in pixels.
        source_height: Source scan height in pixels.
        timings: Load, register and measure durations, already finished -
            :func:`_decide_and_build` adds its own decide/present durations to
            these rather than restarting the clock, so a session's later
            recomputes report only the (small) cost they actually paid.
    """

    source_path: Path
    image: NDArray[np.uint8]
    alignment: AlignmentResult
    page: NDArray[np.uint8]
    page_ink: float
    measurements: dict[str, dict[tuple[int, int], BubbleMeasurement]]
    canonical_width: int
    canonical_height: int
    source_width: int
    source_height: int
    timings: StageTimings


class _SheetMeasurementError(Exception):
    """Internal control-flow signal: the sheet could not be measured at all.

    Carries the finished :class:`ScanResult` that fully describes why - a
    failed load or a failed registration - so that both the ordinary one-shot
    path and :class:`CalibrationSession` report the same failure the same
    way, once, rather than each deciding separately what it means.
    """

    def __init__(self, result: ScanResult) -> None:
        super().__init__(result.registration_message or result.error_code)
        self.result = result


def _measure_sheet(
    path: Path, template: OmrTemplate, options: RecognitionOptions
) -> _MeasuredSheet:
    """Load, register and measure one sheet.

    Raises:
        _SheetMeasurementError: The file could not be loaded or the page could not be
            registered. Carries the failed :class:`ScanResult`.
    """
    started = time.perf_counter()
    clock = _StageClock(started)
    identity = _TemplateIdentity.of(template)

    try:
        image = load_scan_image(path, color=False)
    except OMRScannerError as exc:
        _LOGGER.warning("Scan %s could not be loaded: %s", path.name, exc)
        raise _SheetMeasurementError(
            _failed_result(
                path,
                template=identity,
                outcome=RecognitionOutcome.ERROR,
                message=exc.user_message,
                error_code=getattr(exc, "code", "") or "",
                timings=clock.finish(),
                has_zones=bool(template.zones),
            )
        ) from exc
    clock.mark("load")

    source_height, source_width = int(image.shape[0]), int(image.shape[1])

    try:
        alignment = align_sheet(image, config=alignment_config_from_template(template))
    except ImagingError as exc:
        _LOGGER.info("Scan %s failed registration (%s): %s", path.name, exc.code, exc)
        clock.mark("register")
        raise _SheetMeasurementError(
            _failed_result(
                path,
                template=identity,
                outcome=RecognitionOutcome.REGISTRATION_FAILED,
                message=exc.user_message,
                error_code=exc.code,
                timings=clock.finish(),
                has_zones=bool(template.zones),
                source_width=source_width,
                source_height=source_height,
                canonical_width=template.page.canonical_width_px,
                canonical_height=template.page.canonical_height_px,
            )
        ) from exc
    clock.mark("register")

    page = alignment.normalized_image
    canonical_height, canonical_width = int(page.shape[0]), int(page.shape[1])

    # The "what does solid ink look like on this page" estimate is a property of
    # the page, not of a zone, so it is measured once and shared: measuring it
    # per zone would let a zone whose bubbles are all empty set a threshold from
    # its own printed glyphs and read them as marks.
    page_ink = estimate_ink_level(page, config=options.metrics)
    measurements = {
        zone.id: _zone_measurements(
            page,
            zone,
            canonical_width=canonical_width,
            canonical_height=canonical_height,
            ink_level=page_ink,
            metrics_config=options.metrics,
        )
        for zone in template.zones
        if not isinstance(zone.field, IgnoredFieldDefinition)
    }
    clock.mark("measure")

    return _MeasuredSheet(
        source_path=path,
        image=image,
        alignment=alignment,
        page=page,
        page_ink=page_ink,
        measurements=measurements,
        canonical_width=canonical_width,
        canonical_height=canonical_height,
        source_width=source_width,
        source_height=source_height,
        timings=clock.finish(),
    )


def _decide_and_build(
    measured: _MeasuredSheet, template: OmrTemplate, options: RecognitionOptions
) -> ScanResult:
    """Turn a measured sheet into a result, against ``template``'s settings.

    This is the part of the pipeline a recognition-*settings* change is
    allowed to repeat cheaply: it reads ``measured.measurements`` - already
    sampled pixels - and ``template.recognition`` (template-level and any
    per-zone override), never the image. Calling this twice with two
    templates that differ only in :attr:`~omr_scanner.domain.template.OmrTemplate.recognition`
    is exactly what :class:`CalibrationSession.recompute` does, and exactly
    why this function takes the already-measured sheet as a separate
    argument instead of loading and registering it itself.
    """
    identity = _TemplateIdentity.of(template)
    path = measured.source_path
    clock = _StageClock(time.perf_counter())

    recognition = recognise_template(template, measured.measurements)
    clock.mark("decide")

    fields, answers, zones, bubbles = _build_views(
        template,
        recognition,
        measured.measurements,
        canonical_width=measured.canonical_width,
        canonical_height=measured.canonical_height,
        ink_level=measured.page_ink,
        metrics_config=options.metrics,
        keep_measurements=options.keep_bubble_measurements,
    )

    preview: DecodedImage | None = None
    preview_scale = 1.0
    if options.with_preview:
        preview, preview_scale = _downscaled_preview(
            measured.page, options.preview_max_dimension
        )

    quality = (
        _scan_quality(
            measured.alignment,
            measured.page,
            source_width=measured.source_width,
            source_height=measured.source_height,
        )
        if options.keep_quality_metrics
        else None
    )

    warnings = tuple(warning.value for warning in measured.alignment.warnings)
    registration = _registration_status(warnings)
    needs_review = recognition.review_count > 0
    outcome = RecognitionOutcome.REVIEW if needs_review else RecognitionOutcome.COMPLETE
    identifier = next(
        (item for item in fields if item.zone_id == recognition.identifier_zone_id), None
    )
    set_code = next(
        (item for item in fields if item.zone_id == recognition.set_code_zone_id), None
    )
    status_codes = derive_status_codes(
        outcome=outcome,
        registration=registration,
        error_code="",
        identifier=identifier,
        set_code=set_code,
        answers=answers,
        fields_=fields,
        has_zones=bool(template.zones),
    )
    clock.mark("present")
    local_timings = clock.finish()
    base = measured.timings
    timings = StageTimings(
        load=base.load,
        register=base.register,
        measure=base.measure,
        decide=local_timings.decide,
        present=local_timings.present,
        total=base.total + local_timings.total,
    )

    result = ScanResult(
        source_path=path,
        outcome=outcome,
        registration=registration,
        registration_message=_describe_warnings(warnings),
        warnings=warnings,
        status_codes=status_codes,
        fields=fields,
        answers=answers,
        identifier_zone_id=recognition.identifier_zone_id,
        set_code_zone_id=recognition.set_code_zone_id,
        zones=zones,
        bubbles=bubbles,
        markers=tuple(
            _marker_view(detection, template, measured.alignment)
            for detection in measured.alignment.corner_markers
        ),
        preview=preview,
        preview_scale=preview_scale,
        canonical_width=measured.canonical_width,
        canonical_height=measured.canonical_height,
        source_width=measured.source_width,
        source_height=measured.source_height,
        elapsed_seconds=timings.total,
        template_id=identity.identifier,
        template_name=identity.name,
        template_version=identity.version,
        recognised_at=utc_timestamp(),
        quality=quality,
        timings=timings,
        source_transform=_inverse_transform(measured.alignment),
    )

    _LOGGER.info(
        "Scan %s: engine=%s registration=%s warnings=%d review_items=%d "
        "multiple=%d blank=%d status=%s in %.3fs",
        path.name,
        ENGINE_VERSION,
        registration.value,
        len(warnings),
        recognition.review_count,
        recognition.multiple_mark_count,
        recognition.blank_answer_count,
        ",".join(status_codes),
        timings.total,
    )
    _LOGGER.debug(
        "Scan %s timing: load=%.3fs register=%.3fs measure=%.3fs decide=%.3fs present=%.3fs",
        path.name,
        timings.load,
        timings.register,
        timings.measure,
        timings.decide,
        timings.present,
    )

    if options.diagnostics.enabled:
        # Imported here, not at module scope: the diagnostics module draws with
        # OpenCV, and a normal run must not pay for importing a renderer it will
        # never call.
        from omr_scanner.services.recognition_diagnostics import write_diagnostics

        write_diagnostics(
            result,
            options.diagnostics,
            original=measured.image,
            registered=measured.page,
        )

    return result


def _inverse_transform(alignment: AlignmentResult) -> tuple[float, ...]:
    """Return the canonical-to-source homography, row-major, or ``()``.

    The stored transform maps the scan onto the canonical page; its inverse
    maps a canonical coordinate back onto the scan it came from, which is the
    only way anything can point at *where on the original sheet* a disputed
    mark is (Phase 6). Recorded rather than recomputed by the consumer so that
    there is exactly one transform in play and it is the engine's own.

    A singular matrix cannot be inverted. That would mean a degenerate fit,
    which registration would already have rejected - but returning an empty
    tuple rather than raising keeps a reporting concern from ending a batch.
    """
    try:
        inverse = np.linalg.inv(alignment.transform_matrix)
    except np.linalg.LinAlgError:  # pragma: no cover - a valid fit is invertible
        _LOGGER.warning("The alignment transform could not be inverted")
        return ()
    return tuple(float(value) for value in inverse.reshape(-1))


def map_canonical_to_source(
    result: ScanResult, points: Sequence[tuple[float, float]]
) -> tuple[tuple[float, float], ...]:
    """Map canonical page coordinates back onto the original scan's pixels.

    Args:
        result: A result carrying
            :attr:`~omr_scanner.services.recognition_models.ScanResult.source_transform`.
        points: ``(x, y)`` in canonical page pixels.

    Returns:
        The same points in the source image's pixels, or ``()`` when this
        result has no usable transform - a page that never registered, or one
        produced by a build from before the field existed.

    The one place that transform is applied. Callers - the conflict review
    workspace, above all - get source coordinates without importing
    :mod:`omr_scanner.imaging` or doing projective arithmetic of their own,
    which is what keeps the GUI layer free of geometry.
    """
    if len(result.source_transform) != 9 or not points:
        return ()
    matrix = np.asarray(result.source_transform, dtype=np.float64).reshape(3, 3)
    mapped = apply_transform(matrix, [Point(x=x, y=y) for x, y in points])
    return tuple((item.x, item.y) for item in mapped)


def _marker_view(
    detection: RegistrationMarkerDetection, template: OmrTemplate, alignment: AlignmentResult
) -> MarkerView:
    """Build one marker's view, with its canonical and expected positions.

    ``canonical_x/y`` is the *detected* centre, mapped through the same
    homography that produced :attr:`_MeasuredSheet.page` - the fitted
    transform is what makes the rectified page, so reprojecting the marker
    that fitted it is not a second computation, only the same one applied to
    one more point. ``expected_x/y`` needs no transform at all: it is simply
    the template's own declared marker centre, scaled onto the canonical page
    it already describes.
    """
    role = detection.role
    canonical = apply_transform(alignment.transform_matrix, [detection.center])[0]
    expected_marker = template.marker_by_role(role)
    expected = Point(
        x=expected_marker.center.x * template.page.canonical_width_px,
        y=expected_marker.center.y * template.page.canonical_height_px,
    )
    return MarkerView(
        role=str(role.value),
        x=detection.center.x,
        y=detection.center.y,
        score=detection.score,
        canonical_x=canonical.x,
        canonical_y=canonical.y,
        expected_x=expected.x,
        expected_y=expected.y,
    )


class CalibrationSession:
    """One scan, registered and measured once; settings re-applied at will.

    Purpose:
        Give the calibration workflow (Phase 4) "change a threshold, see the
        effect at once" without asking it to understand, or duplicate,
        anything about registration or bubble sampling. A session is what
        :meth:`RecognitionEngine.open_session` returns; nothing else
        constructs one.

    What a session may *not* do:
        Change geometry. :meth:`recompute` re-decides the measurements this
        session already took against a new template's turn
        :class:`~omr_scanner.domain.template.RecognitionSettings`; it never
        re-measures. Passing a template whose zones, grids or page differ
        from the one this session was opened with produces a result read
        against measurements taken for different bubbles - which is exactly
        the geometry edit Phase 4 defers to the Template Designer
        (``docs/calibration_workflow.md``) rather than perform itself.

    A failed registration is cached too: :meth:`recompute` on a sheet that
    could not be registered returns the same failed result every time,
    because no recognition setting could have changed why.
    """

    def __init__(
        self,
        measured: _MeasuredSheet | None,
        failure: ScanResult | None,
        options: RecognitionOptions,
    ) -> None:
        self._measured = measured
        self._failure = failure
        self._options = options

    @property
    def source_path(self) -> Path:
        """The scan this session was opened for."""
        if self._measured is not None:
            return self._measured.source_path
        if self._failure is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("A session must carry either a measurement or a failure")
        return self._failure.source_path

    @property
    def registered(self) -> bool:
        """Whether the sheet could be registered at all.

        ``False`` means every call to :meth:`recompute` will return the same
        failed result - there is no threshold that fixes a page that could not
        be rectified.
        """
        return self._measured is not None

    def recompute(self, template: OmrTemplate) -> ScanResult:
        """Decide this session's cached measurements against ``template``.

        Args:
            template: The **same** template this session was opened with,
                possibly carrying different recognition settings
                (template-level or per-zone). Its zones and grids must
                describe the same geometry; see the class docstring.

        Returns:
            The result. Costs one pass over the cached measurements - no file
            read, no marker detection, no perspective warp - which is what
            makes it safe to call once per slider movement.
        """
        if self._measured is None:
            if self._failure is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("A session must carry either a measurement or a failure")
            return self._failure
        return _decide_and_build(self._measured, template, self._options)

    def original_preview(
        self, max_dimension: int | None = DEFAULT_PREVIEW_MAX_DIMENSION
    ) -> tuple[DecodedImage, float, int, int]:
        """A display copy of the scan **before** registration.

        For the calibration viewer's "Original scan" mode
        (``docs/calibration_workflow.md``): the page as it arrived, with none
        of Phase 1's correction applied, so a printer margin or a skewed feed
        is visible for what it is rather than already straightened out.

        Args:
            max_dimension: Longest side of the returned copy; ``None`` for
                full resolution.

        Returns:
            ``(image, scale, source_width, source_height)`` - the scale and
            the *source* dimensions travel with the image because overlay
            coordinates are canonical-page pixels and therefore do **not**
            apply to it. A caller showing this image must say so and draw no
            bubble or zone overlay over it; see
            :meth:`~omr_scanner.gui.calibration.page.CalibrationPage._refresh_preview_mode`.

        Raises:
            RuntimeError: The session's registration failed, so there is no
                cached scan to preview - callers should read
                :attr:`registered` first.
        """
        if self._measured is None:
            raise RuntimeError(
                "This session's registration failed; there is no scan cached to preview"
            )
        image, scale = _downscaled_preview(self._measured.image, max_dimension)
        return image, scale, self._measured.source_width, self._measured.source_height


class _StageClock:
    """Accumulates per-stage durations without littering the pipeline with time.

    One object rather than a scatter of ``t0 = perf_counter()`` pairs, so that
    adding a stage is one call and the timings can never disagree about where
    the boundaries are.
    """

    __slots__ = ("_previous", "_stages", "_started")

    def __init__(self, started: float) -> None:
        self._started = started
        self._previous = started
        self._stages: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        """Record everything since the last mark as ``stage``."""
        now = time.perf_counter()
        self._stages[stage] = self._stages.get(stage, 0.0) + (now - self._previous)
        self._previous = now

    def finish(self) -> StageTimings:
        """Return the timings, including the total elapsed time."""
        return StageTimings(
            load=self._stages.get("load", 0.0),
            register=self._stages.get("register", 0.0),
            measure=self._stages.get("measure", 0.0),
            decide=self._stages.get("decide", 0.0),
            present=self._stages.get("present", 0.0),
            total=time.perf_counter() - self._started,
        )


class _TemplateIdentity:
    """The three identifying values a result records about its template.

    Extracted once, defensively: a result must still be produced when the
    template is unusual, and reaching into ``template.page`` in the middle of a
    failure path is how a reporting bug turns into a crashed batch.
    """

    __slots__ = ("identifier", "name", "version")

    def __init__(self, identifier: str, name: str, version: int) -> None:
        self.identifier = identifier
        self.name = name
        self.version = version

    @classmethod
    def of(cls, template: OmrTemplate) -> _TemplateIdentity:
        """Read the identity from a template."""
        return cls(
            identifier=str(getattr(template, "template_id", "")),
            name=str(getattr(template, "name", "")),
            version=int(getattr(template, "format_version", 0)),
        )


def _failed_result(
    path: Path,
    *,
    template: _TemplateIdentity,
    outcome: RecognitionOutcome,
    message: str,
    error_code: str,
    timings: StageTimings,
    has_zones: bool,
    source_width: int = 0,
    source_height: int = 0,
    canonical_width: int = 0,
    canonical_height: int = 0,
) -> ScanResult:
    """Build the result that stands for "this sheet could not be read".

    One constructor for every failure path, so that a failed result is as fully
    described as a successful one - same engine stamp, same timestamp, same
    status codes - instead of being a stub that later phases have to special
    case.
    """
    return ScanResult(
        source_path=path,
        outcome=outcome,
        registration=RegistrationStatus.FAILED,
        registration_message=message,
        error_code=error_code,
        status_codes=derive_status_codes(
            outcome=outcome,
            registration=RegistrationStatus.FAILED,
            error_code=error_code,
            identifier=None,
            set_code=None,
            answers=(),
            fields_=(),
            has_zones=has_zones,
        ),
        source_width=source_width,
        source_height=source_height,
        canonical_width=canonical_width,
        canonical_height=canonical_height,
        elapsed_seconds=timings.total,
        template_id=template.identifier,
        template_name=template.name,
        template_version=template.version,
        recognised_at=utc_timestamp(),
        timings=timings,
    )


def _downscaled_preview(
    image: NDArray[np.uint8], max_dimension: int | None
) -> tuple[DecodedImage, float]:
    """Return a display copy of the rectified page and its scale factor.

    Uses area averaging, which is the correct filter for shrinking: it keeps a
    faint pencil mark visible instead of letting point sampling miss it.
    """
    import cv2  # local import: only the preview path needs OpenCV here

    height, width = int(image.shape[0]), int(image.shape[1])
    scale = 1.0
    if max_dimension is not None and max(width, height) > max_dimension:
        scale = max_dimension / float(max(width, height))
        target = (max(round(width * scale), 1), max(round(height * scale), 1))
        resized = cv2.resize(image, target, interpolation=cv2.INTER_AREA)
    else:
        resized = image

    contiguous = np.ascontiguousarray(resized)
    out_height, out_width = int(contiguous.shape[0]), int(contiguous.shape[1])
    channels = int(contiguous.shape[2]) if contiguous.ndim == 3 else 1
    return (
        DecodedImage(
            width=out_width,
            height=out_height,
            channels=channels,
            stride=out_width * channels,
            data=contiguous.tobytes(),
        ),
        scale,
    )


def _zone_measurements(
    page: NDArray[np.uint8],
    zone: Zone,
    *,
    canonical_width: int,
    canonical_height: int,
    ink_level: float,
    metrics_config: BubbleMetricsConfig,
) -> dict[tuple[int, int], BubbleMeasurement]:
    """Measure every bubble of one zone, keyed by its ``(row, column)``.

    The template stores normalised coordinates; this is the single place they
    become canonical pixels, and it is deliberately arithmetic rather than a
    search: the page has already been rectified onto exactly those coordinates,
    so a bubble that is not where the template says it is means the template is
    wrong, not that the sheet should be hunted through.
    """
    grid = zone.grid
    if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
        return {}

    width_px = grid.bubble_size.width * canonical_width
    height_px = grid.bubble_size.height * canonical_height

    cells = [
        (row, column)
        for row in range(zone.field.rows)
        for column in range(zone.field.columns)
    ]
    centers = []
    for row, column in cells:
        center = grid.bubble_center(row, column)
        centers.append((center.x * canonical_width, center.y * canonical_height))

    measurements = measure_bubbles(
        page,
        centers,
        width_px=width_px,
        height_px=height_px,
        ink_level=ink_level,
        config=metrics_config,
    )
    return dict(zip(cells, measurements, strict=True))


def _build_views(
    template: OmrTemplate,
    recognition: SheetRecognition,
    measurements: dict[str, dict[tuple[int, int], BubbleMeasurement]],
    *,
    canonical_width: int,
    canonical_height: int,
    ink_level: float,
    metrics_config: BubbleMetricsConfig,
    keep_measurements: bool = True,
) -> tuple[
    tuple[FieldView, ...],
    tuple[AnswerView, ...],
    tuple[ZoneView, ...],
    tuple[BubbleView, ...],
]:
    """Project a recognition result into the plain types the GUI consumes."""
    fields = tuple(
        FieldView(
            zone_id=item.zone_id,
            label=item.label,
            field_type=item.field_type,
            value=item.value,
            status=item.status.value,
            needs_review=item.needs_review,
            characters=tuple(
                CharacterView(
                    position=position,
                    value=group.value,
                    status=group.status.value,
                    top_fill=group.top_fill,
                    margin=group.margin,
                    confidence=group.confidence,
                )
                for position, group in enumerate(item.groups)
            ),
        )
        for item in recognition.fields
    )

    answers = tuple(
        AnswerView(
            number=answer.number,
            zone_id=answer.zone_id,
            value=answer.value,
            status=answer.status.value,
            needs_review=answer.needs_review,
            top_fill=answer.decision.top_fill,
            margin=answer.decision.margin,
            confidence=answer.decision.confidence,
        )
        for answer in recognition.answers
    )

    # Per-zone worst status, so one overlay rectangle can carry the state of a
    # whole question block without the GUI scanning every answer itself.
    zone_status: dict[str, str] = {item.zone_id: item.status.value for item in recognition.fields}
    for answer in recognition.answers:
        current = zone_status.get(answer.zone_id)
        zone_status[answer.zone_id] = _worse_status(current, answer.status.value)

    zones = tuple(
        ZoneView(
            zone_id=zone.id,
            label=zone.label,
            field_type=str(zone.field.type.value),
            x=zone.bounds.x * canonical_width,
            y=zone.bounds.y * canonical_height,
            width=zone.bounds.width * canonical_width,
            height=zone.bounds.height * canonical_height,
            color=zone.display_color,
            status=zone_status.get(zone.id, ""),
        )
        for zone in template.zones
    )

    # Which bubbles the decision layer selected, how each group ended up, and
    # where each bubble ranked inside its group. `zone_groups` yields groups in
    # the same order the recognition layer decided them, so the two zip together
    # without any lookup key.
    selected_cells: dict[str, set[tuple[int, int]]] = {}
    leading_cells: dict[str, set[tuple[int, int]]] = {}
    cell_status: dict[str, dict[tuple[int, int], str]] = {}
    cell_rank: dict[str, dict[tuple[int, int], int]] = {}
    field_groups = {item.zone_id: item.groups for item in recognition.fields}

    for zone in template.zones:
        if isinstance(zone.field, IgnoredFieldDefinition):
            continue
        decisions = field_groups.get(zone.id)
        if decisions is None:
            decisions = tuple(
                answer.decision
                for answer in sorted(
                    (a for a in recognition.answers if a.zone_id == zone.id),
                    key=lambda answer: answer.number,
                )
            )
        chosen = selected_cells.setdefault(zone.id, set())
        statuses = cell_status.setdefault(zone.id, {})
        leaders = leading_cells.setdefault(zone.id, set())
        ranks = cell_rank.setdefault(zone.id, {})
        for group, decision in zip(zone_groups(zone), decisions, strict=True):
            order = sorted(
                range(len(group.cells)),
                key=lambda index: -decision.readings[index].fill_ratio,
            )
            for position, index in enumerate(order):
                ranks[group.cells[index]] = position
            for index, cell in enumerate(group.cells):
                statuses[cell] = decision.status.value
                if index in decision.selected:
                    chosen.add(cell)
                if index == decision.leading_index:
                    leaders.add(cell)

    if not keep_measurements:
        # Not merely zeroed - omitted. The bubble records *are* the memory: a
        # hundred-question sheet has five hundred of them against a hundred
        # answers, so a caller processing ten thousand sheets and keeping the
        # results (the Scan page does, for the CSV) saves an order of
        # magnitude by declining them. Every decision has already been made;
        # this drops only the evidence for it.
        return fields, answers, zones, ()

    bubbles: list[BubbleView] = []
    for zone in template.zones:
        grid = zone.grid
        if grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            continue
        labels_by_cell = {
            cell: group.labels[index]
            for group in zone_groups(zone)
            for index, cell in enumerate(group.cells)
        }
        zone_measure = measurements.get(zone.id, {})
        for (row, column), measurement in sorted(zone_measure.items()):
            evidence = (
                _bubble_evidence(measurement, ink_level=ink_level, config=metrics_config)
                if keep_measurements
                else _EMPTY_EVIDENCE
            )
            bubbles.append(
                BubbleView(
                    zone_id=zone.id,
                    row=row,
                    column=column,
                    label=labels_by_cell.get((row, column), ""),
                    x=measurement.center_x,
                    y=measurement.center_y,
                    width=grid.bubble_size.width * canonical_width,
                    height=grid.bubble_size.height * canonical_height,
                    fill_ratio=measurement.fill_ratio,
                    selected=(row, column) in selected_cells.get(zone.id, set()),
                    leading=(row, column) in leading_cells.get(zone.id, set()),
                    group_status=cell_status.get(zone.id, {}).get((row, column), ""),
                    rank=cell_rank.get(zone.id, {}).get((row, column), 0),
                    mean_darkness=evidence.mean_darkness,
                    contrast=evidence.contrast,
                    paper_level=evidence.paper_level,
                    ink_threshold=evidence.ink_threshold,
                    sample_pixels=evidence.sample_pixels,
                    usable=evidence.usable,
                    # Straight off the measurement, never recomputed from the
                    # grid: this is the ellipse the sampler read, and a
                    # calibration overlay draws it to show the operator the
                    # actual region behind `fill_ratio`.
                    sample_half_width=measurement.sample_half_width,
                    sample_half_height=measurement.sample_half_height,
                )
            )

    return fields, answers, zones, tuple(bubbles)


class _Evidence(NamedTuple):
    """The measured numbers a bubble view carries alongside its fill ratio."""

    mean_darkness: float
    contrast: float
    paper_level: float
    ink_threshold: float
    sample_pixels: int
    usable: bool


_EMPTY_EVIDENCE = _Evidence(0.0, 0.0, 0.0, 0.0, 0, True)
"""What a bubble carries when the caller asked not to keep the evidence."""


def _bubble_evidence(
    measurement: BubbleMeasurement,
    *,
    ink_level: float,
    config: BubbleMetricsConfig,
) -> _Evidence:
    """Return the measured evidence behind one bubble, ready for a view.

    The threshold is recomputed rather than stored by the measurement because it
    is a *derived* quantity - local paper level, the page's ink level and the
    configured fraction - and duplicating it inside every measurement would make
    the imaging layer responsible for a value only a report cares about.
    """
    return _Evidence(
        mean_darkness=measurement.mean_darkness,
        contrast=measurement.contrast,
        paper_level=measurement.paper_level,
        ink_threshold=ink_threshold(measurement.paper_level, ink_level, config),
        sample_pixels=measurement.sample_pixels,
        usable=measurement.usable,
    )


_STATUS_SEVERITY: dict[str, int] = {
    FieldStatus.RESOLVED.value: 0,
    MarkStatus.RESOLVED.value: 0,
    FieldStatus.BLANK.value: 1,
    MarkStatus.BLANK.value: 1,
    FieldStatus.INCOMPLETE.value: 2,
    FieldStatus.UNCERTAIN.value: 3,
    MarkStatus.UNCERTAIN.value: 3,
    FieldStatus.MULTIPLE.value: 4,
    MarkStatus.MULTIPLE.value: 4,
    FieldStatus.UNREADABLE.value: 5,
    MarkStatus.UNREADABLE.value: 5,
}
"""How loudly each status should speak when several are summarised into one.

Ordered by how much human attention the state deserves, not alphabetically: an
unreadable region outranks a double mark, which outranks a faint one, which
outranks a gap."""


def _worse_status(current: str | None, candidate: str) -> str:
    """Return whichever of two status strings deserves more attention."""
    if current is None:
        return candidate
    if _STATUS_SEVERITY.get(candidate, 0) > _STATUS_SEVERITY.get(current, 0):
        return candidate
    return current


def _registration_status(warnings: tuple[str, ...]) -> RegistrationStatus:
    """Map alignment warnings onto a registration status."""
    if warnings:
        return RegistrationStatus.REGISTERED_WITH_WARNING
    return RegistrationStatus.REGISTERED


def _describe_warnings(warnings: tuple[str, ...]) -> str:
    """Render alignment warnings as one plain-language sentence."""
    if not warnings:
        return ""
    readable = ", ".join(warning.replace("_", " ").lower() for warning in warnings)
    return f"Registered, but with reservations: {readable}."


# ----------------------------------------------------------------------
# Scan quality: measured, reported, never consulted
# ----------------------------------------------------------------------
def _scan_quality(
    alignment: AlignmentResult,
    page: NDArray[np.uint8],
    *,
    source_width: int,
    source_height: int,
) -> ScanQuality:
    """Summarise how good this scan was, for diagnosis rather than judgement."""
    metrics = alignment.metrics
    rotation, skew, perspective = _transform_geometry(
        alignment.transform_matrix, width=source_width, height=source_height
    )
    brightness, contrast, sharpness = _image_statistics(page)
    return ScanQuality(
        marker_count=len(alignment.corner_markers),
        min_marker_score=metrics.min_marker_score,
        mean_reprojection_error_px=metrics.mean_reprojection_error_px,
        max_reprojection_error_px=metrics.max_reprojection_error_px,
        aspect_ratio_deviation=metrics.aspect_ratio_deviation,
        quadrilateral_area_ratio=metrics.quadrilateral_area_ratio,
        quarter_turns=alignment.orientation.quarter_turns,
        rotation_degrees=rotation,
        skew_degrees=skew,
        perspective_strength=perspective,
        orientation_confidence=metrics.orientation_confidence,
        orientation_assumed=alignment.orientation.assumed,
        source_width=source_width,
        source_height=source_height,
        brightness=brightness,
        contrast=contrast,
        sharpness=sharpness,
        working_scale=metrics.working_scale,
    )


def _transform_geometry(
    matrix: NDArray[np.float64], *, width: int, height: int
) -> tuple[float, float, float]:
    """Estimate rotation, skew and perspective strength from the homography.

    The affine part of the matrix is decomposed the standard way: the first
    column's angle is the rotation, and the departure of the two columns from
    perpendicularity is the skew. The projective row is scaled by the page's own
    size, which turns it into "how many times the scale changes across the
    page" - a number that means the same thing at any resolution, unlike the raw
    coefficients.

    These are *descriptions of the correction that was applied*, not error
    measurements. A page photographed at an angle produces a large perspective
    strength and a perfectly good result.
    """
    a, b = float(matrix[0, 0]), float(matrix[0, 1])
    c, d = float(matrix[1, 0]), float(matrix[1, 1])

    rotation = math.degrees(math.atan2(c, a))
    # Fold into [-45, 45]: whole quarter turns are reported separately, and a
    # 90-degree "rotation" here would be that, not a skewed page.
    while rotation > 45.0:
        rotation -= 90.0
    while rotation < -45.0:
        rotation += 90.0

    column_x = math.hypot(a, c)
    column_y = math.hypot(b, d)
    if column_x <= 0.0 or column_y <= 0.0:
        skew = 0.0
    else:
        cosine = (a * b + c * d) / (column_x * column_y)
        skew = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cosine))))

    scale = float(matrix[2, 2]) or 1.0
    perspective = (
        abs(float(matrix[2, 0])) * max(width, 1) + abs(float(matrix[2, 1])) * max(height, 1)
    ) / abs(scale)
    return rotation, skew, perspective


def _image_statistics(page: NDArray[np.uint8]) -> tuple[float, float, float]:
    """Return ``(brightness, contrast, sharpness)`` of the rectified page.

    Computed on a bounded downsample so the cost does not grow with scanner
    resolution: these are summaries for a human reading a report, and a
    half-resolution page has the same mean and a proportional Laplacian
    variance.
    """
    import cv2  # local import: only this diagnostic path needs OpenCV

    sample: NDArray[np.uint8] = page
    longest = max(int(page.shape[0]), int(page.shape[1]))
    if longest > DEFAULT_QUALITY_SAMPLE_MAX_DIMENSION:
        factor = DEFAULT_QUALITY_SAMPLE_MAX_DIMENSION / float(longest)
        sample = cast(
            "NDArray[np.uint8]",
            cv2.resize(
                page,
                (
                    max(round(int(page.shape[1]) * factor), 1),
                    max(round(int(page.shape[0]) * factor), 1),
                ),
                interpolation=cv2.INTER_AREA,
            ),
        )

    values = sample.astype(np.float32)
    brightness = float(values.mean()) / 255.0
    contrast = float(values.std()) / 255.0
    laplacian = cv2.Laplacian(sample, cv2.CV_32F)
    sharpness = float(laplacian.var()) / (255.0 * 255.0)
    return brightness, contrast, sharpness


__all__ = [
    "DEFAULT_PREVIEW_MAX_DIMENSION",
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "RESULT_SCHEMA_VERSION",
    "AnswerView",
    "BubbleView",
    "CalibrationSession",
    "CharacterView",
    "DiagnosticsOptions",
    "FieldView",
    "MarkerView",
    "RecognitionEngine",
    "RecognitionOptions",
    "RecognitionOutcome",
    "RegistrationStatus",
    "ScanQuality",
    "ScanResult",
    "StageTimings",
    "StatusCode",
    "ZoneView",
    "map_canonical_to_source",
    "recognise_scan",
]
