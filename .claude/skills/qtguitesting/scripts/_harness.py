"""Shared plumbing for the qtguitesting helper scripts.

Purpose:
    Build a real, deterministic `TemplateDesignerPage` with a document open, so
    the three scripts that need one (screenshots, geometry dumps, the smoke test)
    agree on what "the Template page with the sample loaded" means instead of
    each assembling it slightly differently.

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


def orientation_search_rect() -> tuple[float, float, float, float]:
    """A plausible hand-drawn search rectangle around the sample's dash, in px.

    Deliberately loose and off-centre, the way a person drags one - the detector
    must not need a tight or centred box. Diagnostic only.
    """
    return (110.0, 255.0, 250.0, 175.0)
