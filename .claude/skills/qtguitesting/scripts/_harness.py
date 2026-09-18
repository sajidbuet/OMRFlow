"""Shared plumbing for the qtguitesting helper scripts.

Purpose:
    Build real, deterministic OMRFlow pages with a document open, so the three
    scripts that need one (screenshots, geometry dumps, the smoke test) agree on
    what "the Template page with the sample loaded" - or "the Scan page with a
    batch processed" - means instead of each assembling it slightly differently.

    Two harnesses, because the two pages are driven differently:
    :class:`DesignerHarness` (Phase 2) edits a template in place, while
    :class:`ScanHarness` (Phase 3) runs a background `QThread` and must be
    *waited on* rather than merely settled.

What does NOT belong here:
    * Any behaviour the application does not have. These scripts drive the real
      widgets through the real page methods; nothing here may exist only for the
      benefit of a test (see `SKILL.md`, "Never do this").
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
"""The repository root, derived from this file's own location.

Four levels up from ``.claude/skills/qtguitesting/scripts/_harness.py``. Never
from the working directory: these scripts are run from wherever the developer -
or the agent - happens to be."""

SAMPLE_SHEET = REPOSITORY_ROOT / "examples" / "ECE-0000.png"
"""OMRFlow's standard real-image GUI regression sample. Read only."""

SAMPLE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
"""The template that describes :data:`SAMPLE_SHEET`.

Pairing the two is what makes the Scan scenarios *real*: the page is driven with
an actual scan and the actual template it was printed from, not a synthetic
render of the application's own idea of a sheet."""

OUTPUT_ROOT = REPOSITORY_ROOT / "test-output" / "gui"
"""Where generated artefacts go. Git-ignored; never mixed with the hand-authored
documentation assets in ``docs/``."""

FAILURE_ROOT = OUTPUT_ROOT / "failures"

WINDOW_WIDTH = 1600
WINDOW_HEIGHT = 1000
"""A fixed window size, so a capture does not depend on the developer's screen."""

# The scripts import `omr_scanner` from the checkout they live in, whether or not
# it happens to be pip-installed in the active interpreter.
_SRC = REPOSITORY_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@dataclass(frozen=True, slots=True)
class DesignerHarness:
    """A Template Designer page with a document open, ready to drive.

    Attributes:
        page: The real `TemplateDesignerPage`.
        image_width: Reference image width in pixels.
        image_height: Reference image height in pixels.
    """

    page: object
    image_width: int
    image_height: int

    @property
    def state(self) -> object:
        """The page's `DesignerState`."""
        return self.page._designer_state

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting before anything is measured.

        Several rounds rather than one: a layout change can queue another, and a
        `QGraphicsView` repaint is queued behind that. Event-driven, never a
        sleep.
        """
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    def settle(self) -> None:
        """Give the page its real laid-out geometry, without showing a window.

        A widget that has never been shown has not had geometry propagated to its
        children: a `QSplitter` in particular keeps its default sizes, so the
        canvas reports something like 150x480 inside a 1600x1000 page. Anything
        that *divides* by a viewport size then - notably
        `TemplateCanvasView.fit_to_window` - computes a zoom for a window that
        does not exist, and the sheet is captured as a postage stamp in an empty
        canvas. ``layout().activate()`` alone does not fix it; the splitter needs
        a real show/resize cycle.

        ``WA_DontShowOnScreen`` is Qt's own answer: ``show()`` runs the whole
        polish, layout and geometry-propagation path, but the window is never
        mapped, so nothing appears on the developer's desktop and the script works
        identically headless. Font metrics and styling stay those of the real
        platform plugin, which is the point of capturing at all.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        widget: QWidget = self.page  # type: ignore[assignment]
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.show()
        widget.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.process_events()


def ensure_application() -> object:
    """Return the running `QApplication`, creating one if needed."""
    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is not None:
        return existing
    return QApplication([])


def build_designer(
    image_path: Path | None = None, *, template_name: str = "GUI capture"
) -> DesignerHarness:
    """Build a Template Designer page with ``image_path`` loaded.

    Uses the same construction path `TemplateDesignerPage.new_template_from_image`
    uses, minus the file-picker dialog - which cannot be driven offscreen and has
    nothing to do with what is being captured.

    Args:
        image_path: Reference sheet to load; the repository sample by default.
        template_name: Name for the new template.

    Returns:
        The harness.

    Raises:
        FileNotFoundError: The image does not exist.
    """
    from omr_scanner.domain.template_authoring import build_blank_template
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.template_designer.page import TemplateDesignerPage
    from omr_scanner.gui.template_designer.state import DesignerState
    from omr_scanner.services import decode_image_file

    path = image_path if image_path is not None else SAMPLE_SHEET
    if not path.is_file():
        raise FileNotFoundError(f"Reference image not found: {path}")

    spec = next(item for item in WORKFLOW_PAGES if item.key == "template")
    page = TemplateDesignerPage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

    decoded = decode_image_file(path)
    template = build_blank_template(
        name=template_name,
        canonical_width_px=decoded.width,
        canonical_height_px=decoded.height,
    )
    page._designer_state = DesignerState(
        template, template_path=None, reference_image_path=path
    )
    page._decoded_image = decoded
    page.canvas.set_reference_image(decoded)
    page.properties.set_image_size(decoded.width, decoded.height)
    page._set_document_controls_enabled(True)
    page._refresh_all()

    harness = DesignerHarness(
        page=page, image_width=decoded.width, image_height=decoded.height
    )
    # Lay out first, *then* fit: `fit_to_window` divides by the viewport's size.
    harness.settle()
    page.canvas.fit_to_window()
    harness.process_events()
    return harness


def build_empty_designer() -> DesignerHarness:
    """A Template Designer page with no document - the empty state."""
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.template_designer.page import TemplateDesignerPage

    spec = next(item for item in WORKFLOW_PAGES if item.key == "template")
    page = TemplateDesignerPage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    harness = DesignerHarness(page=page, image_width=0, image_height=0)
    harness.settle()
    return harness


def question_region_bounds() -> object:
    """The question area of ``examples/ECE-0000.png``, as a normalised rectangle.

    Measured once from the sample (the five printed answer columns span roughly
    x 175-2300, y 2075-3080 of 2480 x 3508) and used only by these *diagnostic*
    scripts - never by anything under ``src/``, which must never carry
    coordinates specific to one image.
    """
    from omr_scanner.domain.geometry import NormalizedRect

    return NormalizedRect(x=0.070, y=0.592, width=0.857, height=0.287)


def add_question_region(
    harness: DesignerHarness, *, columns: int = 5, questions: int = 100
) -> tuple[object, ...]:
    """Create a Question Region over the sample's answer area.

    Built through the real `QuestionBlockDialog` and the real
    `DesignerState.apply_zones`, so what is captured is what the application
    produces - the dialog is simply never `exec()`d, per ``docs/TESTING.md``.
    """
    from omr_scanner.gui.template_designer.dialogs import QuestionBlockDialog

    page = harness.page
    dialog = QuestionBlockDialog(
        bounds=question_region_bounds(),
        existing_zone_ids=[zone.id for zone in page._designer_state.template.zones],
        image_width=harness.image_width,
        image_height=harness.image_height,
        bubble_radius_px=page._default_bubble_radius_px(),
    )
    dialog.labels_edit.setText("a,b,c,d")
    dialog.question_count_box.setValue(questions)
    dialog.columns_box.setValue(columns)
    zones = dialog._build_zones()
    page._designer_state.apply_zones(zones)
    page._refresh_all()
    harness.process_events()
    return zones


@dataclass(frozen=True, slots=True)
class ScanHarness:
    """A Scan page with a template loaded and scans imported, ready to drive.

    Attributes:
        page: The real `ScanPage`.
        template_path: The ``.omrt`` it loaded.
        output_dir: Where renamed copies go, when renaming is switched on.
    """

    page: object
    template_path: Path
    output_dir: Path

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting. See `DesignerHarness`."""
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    def settle(self) -> None:
        """Give the page real laid-out geometry without showing a window."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        widget: QWidget = self.page  # type: ignore[assignment]
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.show()
        widget.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.process_events()

    def run_batch(self, *, timeout_ms: int = 120_000) -> object:
        """Process every imported scan and return the `BatchReport`.

        Pumps the event loop until the page's own ``batch_finished`` signal
        arrives, which is both the honest definition of "the run is over" and
        the reason this is not a sleep: the worker is a real ``QThread``, and a
        fixed delay would be simultaneously slower than necessary and unreliable
        on a loaded machine.

        Raises:
            TimeoutError: The run did not finish inside ``timeout_ms``.
        """
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        received: list[object] = []
        self.page.batch_finished.connect(received.append)
        try:
            if not self.page.process_all():
                raise RuntimeError(
                    "the batch did not start - is a template loaded and a scan imported?"
                )
            clock = QElapsedTimer()
            clock.start()
            while not received:
                QApplication.processEvents()
                if clock.elapsed() > timeout_ms:
                    raise TimeoutError(f"batch did not finish within {timeout_ms} ms")
        finally:
            self.page.batch_finished.disconnect(received.append)
        self.process_events()
        return received[0]

    def shutdown(self) -> None:
        """Stop the page's background threads before the process exits.

        Selecting a row starts a `PreviewWorker`, and a batch finishing selects
        one by itself. A ``QThread`` still running when the interpreter tears
        down makes Qt abort the process - on Windows with a bare
        ``0xC0000409`` and no traceback, which is a thoroughly confusing way for
        a capture run to end after it has already written its files.

        The application itself does this in ``ScanPage.closeEvent``; a script
        that never closes the page has to do it explicitly.
        """
        self.page.close()
        self.process_events()

    def await_preview(self, *, timeout_ms: int = 120_000) -> bool:
        """Pump events until the selected scan's preview has been rendered.

        A batch deliberately discards previews, so selecting a row starts a
        `PreviewWorker`; anything that captures or measures the preview must
        wait for it rather than grabbing an empty view.
        """
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        clock = QElapsedTimer()
        clock.start()
        while not self.page.preview.has_page:
            QApplication.processEvents()
            if clock.elapsed() > timeout_ms:
                return False
        self.process_events()
        return True


def build_scan_page(
    scans: list[Path] | None = None,
    *,
    template_path: Path | None = None,
    output_dir: Path | None = None,
    rename: bool = False,
    processing: object | None = None,
) -> ScanHarness:
    """Build a Scan page with a template loaded and ``scans`` imported.

    Uses the page's own public commands - ``load_template_from``,
    ``add_scan_paths``, ``set_output_directory`` - which are exactly what the
    toolbar buttons call once their file dialog has returned. The dialogs
    themselves are skipped because they cannot be driven offscreen and have
    nothing to do with what is being checked (`docs/TESTING.md`).

    Args:
        scans: Images to import; the repository sample by default.
        template_path: The ``.omrt`` to read them with; the sample's own by
            default.
        output_dir: Folder for renamed copies. Defaults to a fresh directory
            under ``test-output/gui/``.
        rename: Switch on roll-number renaming.
        processing: A `ProcessingSettings` to drive the page with, for the
            multicore scenarios. ``None`` leaves the page on its default
            (Automatic), which is what a fresh installation uses.

    Returns:
        The harness, already laid out.

    Raises:
        FileNotFoundError: The template or a scan does not exist.
        RuntimeError: The template could not be loaded.
    """
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanPage

    template = template_path if template_path is not None else SAMPLE_TEMPLATE
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}")
    selected = list(scans) if scans is not None else [SAMPLE_SHEET]
    for path in selected:
        if not path.is_file():
            raise FileNotFoundError(f"Scan not found: {path}")

    destination = output_dir if output_dir is not None else OUTPUT_ROOT / "scan_output"
    destination.mkdir(parents=True, exist_ok=True)

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    if not page.load_template_from(template):
        raise RuntimeError(f"Could not load the template: {template}")
    page.add_scan_paths(selected)
    page.set_output_directory(destination)
    page.rename_checkbox.setChecked(rename)
    if processing is not None:
        page.set_processing_settings(processing)

    harness = ScanHarness(page=page, template_path=template, output_dir=destination)
    harness.settle()
    return harness


@dataclass(frozen=True, slots=True)
class CalibrationHarness:
    """A Calibration page with a template loaded and scans added, ready to run.

    Attributes:
        page: The real `CalibrationPage`.
        template_path: The ``.omrt`` it loaded.
    """

    page: object
    template_path: Path

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting. See `DesignerHarness`."""
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    def settle(self) -> None:
        """Give the page real laid-out geometry without showing a window."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        widget: QWidget = self.page  # type: ignore[assignment]
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.show()
        widget.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.process_events()

    def run_all(self, *, timeout_ms: int = 120_000) -> None:
        """Register and measure every added scan, waiting on ``run_finished``.

        The same real-``QThread``-plus-pumped-event-loop shape as
        `ScanHarness.run_batch`, for the same reason: the worker is real, so a
        fixed sleep would be both slower than necessary and unreliable.

        Raises:
            RuntimeError: The run did not start.
            TimeoutError: It did not finish inside ``timeout_ms``.
        """
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        # `run_finished` carries no payload (see the page docstring), so the
        # callback takes no argument either - unlike `ScanHarness.run_batch`,
        # which connects straight to `received.append` because
        # `batch_finished` *does* carry the `BatchReport`.
        done = {"finished": False}

        def mark_done() -> None:
            done["finished"] = True

        self.page.run_finished.connect(mark_done)
        try:
            if not self.page.run_all():
                raise RuntimeError(
                    "calibration did not start - is a template loaded and a scan added?"
                )
            clock = QElapsedTimer()
            clock.start()
            while not done["finished"]:
                QApplication.processEvents()
                if clock.elapsed() > timeout_ms:
                    raise TimeoutError(f"calibration run did not finish within {timeout_ms} ms")
        finally:
            self.page.run_finished.disconnect(mark_done)
        self.process_events()

    def shutdown(self) -> None:
        """Stop the page's background thread before the process exits.

        See `ScanHarness.shutdown` for why this matters on Windows.
        """
        self.page.close()
        self.process_events()


def build_calibration_page(
    scans: list[Path] | None = None, *, template_path: Path | None = None
) -> CalibrationHarness:
    """Build a Calibration page with a template loaded and ``scans`` added.

    Uses the page's own public commands - ``load_template_from``,
    ``add_scan_paths`` - exactly as `build_scan_page` does, and for the same
    reason (`docs/TESTING.md`).

    Args:
        scans: Images to add; the repository sample by default.
        template_path: The ``.omrt`` to read them with; the sample's own by
            default.

    Returns:
        The harness, already laid out.

    Raises:
        FileNotFoundError: The template or a scan does not exist.
        RuntimeError: The template could not be loaded.
    """
    from omr_scanner.gui.calibration.page import CalibrationPage
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES

    template = template_path if template_path is not None else SAMPLE_TEMPLATE
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}")
    selected = list(scans) if scans is not None else [SAMPLE_SHEET]
    for path in selected:
        if not path.is_file():
            raise FileNotFoundError(f"Scan not found: {path}")

    spec = next(item for item in WORKFLOW_PAGES if item.key == "calibration")
    page = CalibrationPage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    if not page.load_template_from(template):
        raise RuntimeError(f"Could not load the template: {template}")
    page.add_scan_paths(selected)

    harness = CalibrationHarness(page=page, template_path=template)
    harness.settle()
    return harness


def orientation_search_rect() -> tuple[float, float, float, float]:
    """A plausible hand-drawn search rectangle around the sample's dash, in px.

    Deliberately loose and off-centre, the way a person drags one - the detector
    must not need a tight or centred box. Diagnostic only.
    """
    return (110.0, 255.0, 250.0, 175.0)
