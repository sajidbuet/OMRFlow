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
from collections.abc import Callable
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
    session: object | None = None
    """The open `ProjectSession`, when the page was built with one.

    Phase 5 records every batch into the project database, so a scenario that
    exercises resume, retry or crash recovery needs a real project; one that
    only exercises recognition does not, and passing ``None`` keeps those
    scenarios exactly as fast as they were."""

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting. See `DesignerHarness`."""
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    @property
    def batch_summary(self) -> object | None:
        """The stored counts for the page's current batch, or ``None``."""
        return self.page.batch_summary()

    def run_paths(self, paths: list[Path], *, timeout_ms: int = 120_000) -> object:
        """Process exactly ``paths`` and return the `BatchReport`.

        The partial-run primitive the resume scenarios need: `run_batch`
        always processes the whole list, which by definition leaves nothing to
        resume.
        """
        return self._await_batch(
            lambda: self.page._start_batch(paths), timeout_ms=timeout_ms
        )

    def resume(self, *, timeout_ms: int = 120_000) -> object:
        """Resume the stored batch and return the `BatchReport`."""
        return self._await_batch(self.page.resume_batch, timeout_ms=timeout_ms)

    def retry_failed(self, *, timeout_ms: int = 120_000) -> object:
        """Retry the stored batch's failures and return the `BatchReport`."""
        return self._await_batch(self.page.retry_failed, timeout_ms=timeout_ms)

    def _await_batch(self, start: Callable[[], bool], *, timeout_ms: int) -> object:
        """Start a run with ``start`` and pump events until it finishes."""
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        received: list[object] = []
        self.page.batch_finished.connect(received.append)
        try:
            if not start():
                raise RuntimeError("the batch did not start")
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

        The project session, if there is one, is closed *after* the page: the
        page's own shutdown flushes the last batch results into the database,
        and releasing the SQLite handle first would be closing the file the
        page is still writing to.
        """
        self.page.close()
        self.process_events()
        if self.session is not None:
            self.session.close()

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
    with_project: bool = False,
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
        with_project: Create a throwaway project and open it on the page, so
            that batches are recorded durably (Phase 5). Needed by anything
            exercising resume, retry or crash recovery; skipped otherwise, so
            the recognition scenarios pay nothing for a database they do not
            read.

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

    session = _throwaway_project() if with_project else None
    if session is not None:
        page.on_project_changed(session)

    if not page.load_template_from(template):
        raise RuntimeError(f"Could not load the template: {template}")
    page.add_scan_paths(selected)
    page.set_output_directory(destination)
    page.rename_checkbox.setChecked(rename)
    if processing is not None:
        page.set_processing_settings(processing)

    harness = ScanHarness(
        page=page, template_path=template, output_dir=destination, session=session
    )
    harness.settle()
    return harness


@dataclass(frozen=True, slots=True)
class ReviewHarness:
    """A Resolve page on a batch that genuinely produced conflicts (Phase 6).

    Attributes:
        page: The real `ResolvePage`.
        session: The throwaway project it is reviewing.
        batch_id: The batch whose conflicts are in the queue.
        reviewer: The name decisions are recorded against.
    """

    page: object
    session: object
    batch_id: str
    reviewer: str

    @property
    def database(self) -> object:
        """The project database the decisions land in."""
        return self.session.database

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

    def first_conflict(self) -> object | None:
        """The first conflict in the queue, or ``None`` when there are none."""
        conflicts = self.page.state.conflicts
        return conflicts[0] if conflicts else None

    def select(self, conflict_id: int, *, timeout_ms: int = 120_000) -> bool:
        """Select one conflict and pump events until its sheet is loaded.

        Waits on the *condition* - "this conflict's sheet is the one loaded" -
        rather than on the ``sheet_ready`` signal. The page selects its first
        row as soon as a batch is loaded, so for that conflict the signal has
        usually already fired by the time a script asks for it, and waiting for
        another would wait forever. The condition is true either way.
        """
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        target = next(
            (
                item
                for item in self.page.state.conflicts
                if item.conflict_id == conflict_id
            ),
            None,
        )
        if target is None or not self.page.select_conflict_by_id(conflict_id):
            return False

        clock = QElapsedTimer()
        clock.start()
        while not (
            self.page.state.bundle is not None
            and self.page._loaded_scan_id == target.scan_id
        ):
            QApplication.processEvents()
            if clock.elapsed() > timeout_ms:
                raise TimeoutError(f"the sheet did not load within {timeout_ms} ms")
        self.process_events()
        return True

    def correct(self, value: str, *, expect_failure: bool = False) -> bool:
        """Record a correction, suppressing the error dialog when one is expected."""
        if expect_failure:
            from PySide6.QtWidgets import QMessageBox

            from omr_scanner.gui import error_reporting

            original = QMessageBox.warning
            QMessageBox.warning = staticmethod(  # type: ignore[method-assign]
                lambda *_a, **_k: QMessageBox.StandardButton.Ok
            )
            try:
                return self.page.correct(value)
            finally:
                QMessageBox.warning = original  # type: ignore[method-assign]
                del error_reporting
        return self.page.correct(value)

    def shutdown(self) -> None:
        """Stop the sheet loader, then release the project.

        In that order: the page's own shutdown waits for the worker, and
        releasing the SQLite handle first would close the file something is
        still using.
        """
        self.page.close()
        self.process_events()
        self.session.close()


def build_review_page(
    *, reviewer: str = "Dr. Smoke Test", timeout_ms: int = 120_000
) -> ReviewHarness:
    """Build a Resolve page on a batch that really does contain conflicts.

    Renders three sheets - one with a double-marked question, two sharing a
    roll number - processes them through the real pipeline, detects the
    conflicts the way the Scan page does, and hands back a page showing them.

    Args:
        reviewer: The name decisions are recorded against. Pass ``""`` to
            exercise the "a correction needs a named reviewer" refusal.
        timeout_ms: Unused placeholder kept for symmetry with the other
            harnesses; recognition here is three synthetic sheets.

    Returns:
        The harness, already laid out.
    """
    del timeout_ms
    import cv2

    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.review.page import ResolvePage
    from omr_scanner.services import batch_store, review_store
    from omr_scanner.services.batch_processor import process_batch

    # The synthetic builders live in the repository's own test helpers, which
    # is deliberate: a second sheet generator here would drift from the one the
    # tests use and stop proving anything about the real thing.
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from tests.conftest import build_answer_sheet_template, render_marked_sheet

    template = build_answer_sheet_template()
    session = _throwaway_project()

    scans = OUTPUT_ROOT / "review_scans" / uuid_hex()
    scans.mkdir(parents=True, exist_ok=True)

    def marks(roll: str) -> dict:
        return {
            "roll_number": dict(enumerate(roll)),
            "set_code": {0: "A"},
            "questions_0": dict.fromkeys(range(10), "B"),
            "questions_1": dict.fromkeys(range(10), "C"),
        }

    double = marks("170501")
    double["questions_0"] = {**double["questions_0"], 0: ["B", "D"]}

    paths = []
    for name, sheet in (("double.png", double), ("dup.png", marks("170501"))):
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, sheet))
        paths.append(path)

    database = session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)

    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)

    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    page = ResolvePage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    page.on_project_changed(session)
    page.set_reviewer(reviewer)
    page.load_batch(batch_id, template)

    harness = ReviewHarness(
        page=page, session=session, batch_id=batch_id, reviewer=reviewer
    )
    harness.settle()
    return harness


@dataclass
class ReconciliationHarness:
    """An Attendance page on a roster and a batch that really disagree."""

    page: object
    session: object
    batch_id: str
    roster_id: int
    roster_path: Path
    operator: str

    @property
    def database(self) -> object:
        """The open project's database."""
        return self.session.database

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

    def reconcile(self, *, timeout_ms: int = 120_000) -> bool:
        """Reconcile, and pump events until the worker has finished.

        Reconciliation runs in a ``QThread``, so a scenario that reads the
        table straight after calling this would read the previous run's rows.
        """
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        done: list[bool] = []

        def note() -> None:
            # `reconciled` carries no payload, so the slot must take none.
            done.append(True)

        self.page.reconciled.connect(note)
        try:
            if not self.page.reconcile():
                return False
            clock = QElapsedTimer()
            clock.start()
            while not done:
                QApplication.processEvents()
                if clock.elapsed() > timeout_ms:
                    raise TimeoutError(
                        f"reconciliation did not finish within {timeout_ms} ms"
                    )
        finally:
            self.page.reconciled.disconnect(note)
        self.process_events()
        return True

    def entries(self) -> dict[str, object]:
        """The current table's entries, keyed by the ID they are filed under."""
        return {entry.candidate_id: entry for entry in self.page.state.entries}

    def show_everything(self) -> None:
        """Drop the default "exceptions only" filter."""
        self.page.status_filter.setCurrentIndex(0)
        self.process_events()

    def select(self, candidate_id: str) -> bool:
        """Select the row filed under ``candidate_id``."""
        for row, entry in enumerate(self.page.state.entries):
            if entry.candidate_id == candidate_id:
                self.page.table.selectRow(row)
                self.process_events()
                return True
        return False

    def counts(self) -> object:
        """The stored summary counts for this roster and batch."""
        from omr_scanner.services import reconciliation_store

        return reconciliation_store.stored_counts(
            self.database, self.roster_id, self.batch_id
        )

    def shutdown(self) -> None:
        """Close the page, then the project. Order matters, as in Phase 5."""
        self.page.close()
        self.session.close()


def build_reconciliation_page(
    *, operator: str = "Dr. Smoke Test", timeout_ms: int = 120_000
) -> ReconciliationHarness:
    """Build an Attendance page on the acceptance scenario.

    Renders five sheets through the real pipeline and imports a roster that
    disagrees with them in every way the phase names: a clean match, a
    duplicate, a candidate marked absent who handed one in, one expected who
    did not, and a sheet whose roll number is on nobody's list.

    Args:
        operator: The name decisions are recorded against. Pass ``""`` to
            exercise the "a decision needs a named operator" refusal.
        timeout_ms: How long to wait for the reconciliation worker.

    Returns:
        The harness, already reconciled once.
    """
    import cv2

    from omr_scanner.gui.attendance.page import AttendancePage
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.services import batch_store, reconciliation_store, review_store
    from omr_scanner.services.batch_processor import process_batch
    from omr_scanner.services.candidate_import import read_roster

    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from tests.conftest import build_answer_sheet_template, render_marked_sheet

    template = build_answer_sheet_template()
    session = _throwaway_project()

    work = OUTPUT_ROOT / "reconciliation" / uuid_hex()
    work.mkdir(parents=True, exist_ok=True)

    def marks(roll: str) -> dict:
        return {
            "roll_number": dict(enumerate(roll)),
            "set_code": {0: "A"},
            "questions_0": dict.fromkeys(range(10), "B"),
            "questions_1": dict.fromkeys(range(10), "C"),
        }

    # Deliberately *not* named after their roll numbers. The Phase 3 pipeline
    # logs the file name of a scan it reads - documented, and useful, because
    # it is the operator's own name for their own file. A harness that named
    # its files `m_100001.png` would therefore put "100001" in the log through
    # Phase 3 and make the Phase 7 privacy check assert the wrong thing.
    plan = [
        ("scan_a.png", "100001"),   # matched
        ("scan_b.png", "100002"),   # duplicate pair
        ("scan_c.png", "100002"),
        ("scan_d.png", "100005"),   # marked absent, yet here it is
        ("scan_e.png", "999999"),   # on nobody's list
    ]
    paths = []
    for name, roll in plan:
        path = work / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks(roll)))
        paths.append(path)

    # Every identifier and name below is fictional; a packaged or committed
    # roster must never carry real candidate data.
    roster_path = work / "candidates.csv"
    roster_path.write_text(
        "Sl.No.,Roll No.,Name,Total (90),Merit\n"
        "1,100001,CANDIDATE A,55,1\n"
        "2,100002,CANDIDATE B,60,2\n"
        "3,100003,CANDIDATE C,ABSENT,---\n"
        "4,100004,CANDIDATE D,,---\n"
        "5,100005,CANDIDATE E, abs ,---\n",
        encoding="utf-8",
    )

    database = session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)

    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)

    roster_id = reconciliation_store.import_roster(
        database, read_roster(roster_path), imported_by=operator
    )

    spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
    page = AttendancePage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    page.on_project_changed(session)
    page.set_operator(operator)
    page.set_batch(batch_id)

    harness = ReconciliationHarness(
        page=page,
        session=session,
        batch_id=batch_id,
        roster_id=roster_id,
        roster_path=roster_path,
        operator=operator,
    )
    harness.settle()
    harness.reconcile(timeout_ms=timeout_ms)
    return harness


@dataclass
class ScoringHarness:
    """An Answer Key page and a Results page over one reconciled batch."""

    key_page: object
    results_page: object
    session: object
    batch_id: str
    roster_id: int
    template: object
    plan: object
    operator: str

    @property
    def database(self) -> object:
        """The open project's database."""
        return self.session.database

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting. See `DesignerHarness`."""
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    def settle(self) -> None:
        """Give both pages real laid-out geometry without showing a window."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        for page in (self.key_page, self.results_page):
            widget: QWidget = page  # type: ignore[assignment]
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            widget.show()
            widget.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.process_events()

    def write_key(self, set_code: str, answers: str, *, wrong: str = "") -> bool:
        """Type a key into the page and save it as a new revision."""
        self.key_page.set_combo.setCurrentText(set_code)
        self.key_page.key_edit.setPlainText(answers)
        self.key_page.wrong_edit.setText(wrong)
        self.process_events()
        return bool(self.key_page.save_key())

    def verify_key(self, set_code: str) -> object:
        """Verify the current revision of one set's key, through the store.

        The page's own Verify button opens a confirmation dialog, which these
        scripts never drive; the refusal it enforces (no name, no verification)
        is exercised by `scoring_store` directly.
        """
        from omr_scanner.services import scoring_store

        stored = scoring_store.list_keys(self.database, set_code=set_code)[0]
        return scoring_store.verify_key(
            self.database, stored.key_id, verified_by=self.operator
        )

    def score(self, *, timeout_ms: int = 120_000) -> bool:
        """Score the batch and pump events until the worker has finished."""
        from PySide6.QtCore import QElapsedTimer
        from PySide6.QtWidgets import QApplication

        done: list[bool] = []

        def note() -> None:
            # `scored` carries no payload, so the slot must take none.
            done.append(True)

        self.results_page.scored.connect(note)
        try:
            if not self.results_page.score_batch():
                return False
            clock = QElapsedTimer()
            clock.start()
            while not done:
                QApplication.processEvents()
                if clock.elapsed() > timeout_ms:
                    raise TimeoutError(f"scoring did not finish within {timeout_ms} ms")
        finally:
            self.results_page.scored.disconnect(note)
        self.process_events()
        return True

    def results(self) -> dict[str, object]:
        """The current results table, keyed by candidate."""
        return {
            item.candidate_id: item for item in self.results_page.state.results
        }

    def select(self, candidate_id: str) -> bool:
        """Select one candidate's row in the results table."""
        for row, item in enumerate(self.results_page.state.results):
            if item.candidate_id == candidate_id:
                self.results_page.table.selectRow(row)
                self.process_events()
                return True
        return False

    def shutdown(self) -> None:
        """Close both pages, then the project. Order matters, as in Phase 5."""
        self.results_page.close()
        self.key_page.close()
        self.session.close()


def build_scoring_pages(
    *, operator: str = "Dr. Smoke Test", timeout_ms: int = 120_000
) -> ScoringHarness:
    """Build the Answer Key and Results pages over a reconciled batch.

    Four sheets through the real pipeline, against a roster with one absentee:
    a perfect paper, one with three wrong answers, an absentee, and one sitting
    a set with no key - enough for every outcome the phase distinguishes.

    Args:
        operator: The name verifications and scoring runs are recorded against.
        timeout_ms: How long to wait for the scoring worker.
    """
    import cv2

    from omr_scanner.gui.answer_key.page import AnswerKeyPage
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.results.page import ResultsPage
    from omr_scanner.services import batch_store, reconciliation_store, review_store
    from omr_scanner.services.answer_key import plan_for
    from omr_scanner.services.batch_processor import process_batch
    from omr_scanner.services.candidate_import import read_roster

    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from tests.conftest import build_answer_sheet_template, render_marked_sheet

    template = build_answer_sheet_template()
    plan = plan_for(template)
    session = _throwaway_project()

    work = OUTPUT_ROOT / "scoring" / uuid_hex()
    work.mkdir(parents=True, exist_ok=True)

    def marks(roll: str, set_code: str, wrong: dict[int, str]) -> dict:
        return {
            "roll_number": dict(enumerate(roll)),
            "set_code": {0: set_code},
            "questions_0": {i: wrong.get(i + 1, "A") for i in range(10)},
            "questions_1": {i: wrong.get(i + 11, "A") for i in range(10)},
        }

    # Named neutrally: the Phase 3 pipeline logs each scan's file name, and a
    # fixture named after a roll number would put one in the log.
    plan_rows = [
        ("scan_a.png", "200001", "A", {}),                       # perfect
        ("scan_b.png", "200002", "A", {1: "B", 2: "B", 3: "B"}),  # three wrong
        ("scan_d.png", "200004", "Z", {}),                        # no key for Z
    ]
    paths = []
    for name, roll, set_code, wrong in plan_rows:
        path = work / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks(roll, set_code, wrong)))
        paths.append(path)

    # Every identifier and name below is fictional.
    roster_path = work / "candidates.csv"
    roster_path.write_text(
        "Roll No.,Name,Total (90)\n"
        "200001,CANDIDATE A,55\n"
        "200002,CANDIDATE B,55\n"
        "200003,CANDIDATE C,ABSENT\n"
        "200004,CANDIDATE D,55\n",
        encoding="utf-8",
    )

    database = session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)

    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)
    roster_id = reconciliation_store.import_roster(
        database, read_roster(roster_path), imported_by=operator
    )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)

    key_spec = next(item for item in WORKFLOW_PAGES if item.key == "answer_key")
    key_page = AnswerKeyPage(key_spec)
    key_page.on_project_changed(session)
    key_page.set_reviewer(operator)
    key_page.set_template(template)

    results_spec = next(item for item in WORKFLOW_PAGES if item.key == "results")
    results_page = ResultsPage(results_spec)
    results_page.on_project_changed(session)
    results_page.set_reviewer(operator)
    results_page.set_template(template)
    results_page.set_batch(batch_id)

    harness = ScoringHarness(
        key_page=key_page,
        results_page=results_page,
        session=session,
        batch_id=batch_id,
        roster_id=roster_id,
        template=template,
        plan=plan,
        operator=operator,
    )
    harness.settle()
    del timeout_ms
    return harness


@dataclass(frozen=True, slots=True)
class ReportsHarness:
    """A Reports page over a scored, verified batch.

    Set A's template is already associated - the state "open Result Management
    and generate" starts from.
    """

    page: object
    session: object
    batch_id: str
    roster_id: int
    template: object
    operator: str
    template_path: Path

    @property
    def database(self) -> object:
        """The open project's database."""
        return self.session.database

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

    def shutdown(self) -> None:
        """Close the page, then the project."""
        self.page.close()
        self.session.close()


def build_reports_page(
    *, operator: str = "Dr. Smoke Test", timeout_ms: int = 120_000
) -> ReportsHarness:
    """Build the Reports page over a scored batch, Set A's template associated.

    Reuses :func:`build_scoring_pages`'s exact roster and scan set (Roll
    No./Name/Total (90) - already the phase brief's own sample wording) so a
    Phase 9 smoke check exercises the real Phase 7/8 pipeline underneath it,
    not a synthetic shortcut.
    """
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.reports.page import ReportsPage
    from omr_scanner.services import report_store
    from omr_scanner.services.report_template import suggest_mapping

    scoring = build_scoring_pages(operator=operator, timeout_ms=timeout_ms)
    scoring.write_key("A", "A" * scoring.plan.question_count)
    scoring.verify_key("A")
    scoring.score(timeout_ms=timeout_ms)

    work = OUTPUT_ROOT / "reports" / uuid_hex()
    work.mkdir(parents=True, exist_ok=True)
    template_path = work / "set_a_result.xlsx"
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Rollwise"
    sheet.append(["Sl.No.", "Roll No.", "Name", "Total (90)", "Merit"])
    # Only the candidates genuinely on Set A's own roster: 200001/200002 sat
    # and were scored against Set A; 200003 is the roster's own confirmed
    # absentee (marked here exactly as reconciliation has it, matching a real
    # absentee sheet); 200004 sat a *different* set ("Z", with no key at all)
    # and does not belong on Set A's template - including it would be exactly
    # the "candidate scored against a different set" defect the readiness
    # check exists to catch.
    for index, (roll, name, marks) in enumerate(
        (
            ("200001", "CANDIDATE A", None),
            ("200002", "CANDIDATE B", None),
            ("200003", "CANDIDATE C", "ABSENT"),
        ),
        start=1,
    ):
        sheet.append([index, roll, name, marks, "---" if marks == "ABSENT" else None])
    workbook.save(template_path)
    workbook.close()

    preview_headers = ["Sl.No.", "Roll No.", "Name", "Total (90)", "Merit"]
    mapping = suggest_mapping(preview_headers).to_mapping()
    report_store.associate_template(
        scoring.database, "A", template_path, mapping, sheet_name="Rollwise",
        updated_by=operator,
    )

    reports_spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    reports_page = ReportsPage(reports_spec)
    reports_page.on_project_changed(scoring.session)
    reports_page.set_reviewer(operator)
    reports_page.set_template(scoring.template)
    reports_page.set_batch(scoring.batch_id)

    scoring.key_page.close()
    scoring.results_page.close()

    harness = ReportsHarness(
        page=reports_page, session=scoring.session, batch_id=scoring.batch_id,
        roster_id=scoring.roster_id, template=scoring.template, operator=operator,
        template_path=template_path,
    )
    harness.settle()
    return harness


def uuid_hex() -> str:
    """A short unique directory name, so repeated runs never collide."""
    import uuid

    return uuid.uuid4().hex[:8]


def _throwaway_project() -> object:
    """Create a fresh project under ``test-output/`` and return its session.

    A new directory per call, so one scenario's stored batches can never be
    mistaken for another's - these scripts are run repeatedly and a shared
    project would accumulate batches until "the most recent batch" stopped
    meaning what the scenario intended.
    """
    import shutil
    import uuid

    from omr_scanner.services import create_project

    root = OUTPUT_ROOT / "projects"
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / uuid.uuid4().hex[:8]
    if workspace.exists():  # pragma: no cover - a uuid collision is not expected
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    return create_project(workspace, "GUI Test Examination")


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


@dataclass(frozen=True, slots=True)
class ShellHarness:
    """The whole application window, ready to capture the chrome row from.

    Attributes:
        window: The real `MainWindow`, built with its ordinary frameless
            chrome so that what is captured is what ships.
        project_root: The throwaway project's directory, or ``None`` when the
            window was built with no project open.
    """

    window: object
    project_root: object = None

    def process_events(self, *, rounds: int = 3) -> None:
        """Let Qt finish laying out and painting before anything is measured."""
        from PySide6.QtWidgets import QApplication

        for _ in range(rounds):
            QApplication.processEvents()

    def at_width(self, width: int, height: int = WINDOW_HEIGHT) -> str:
        """Resize the window and return the ribbon layout it adopted.

        Returns the mode's own name, so a capture run prints which of the
        three layouts each screenshot actually shows rather than leaving the
        reader to infer it from the picture.
        """
        self.window.resize(width, height)
        self.process_events()
        return self.window.ribbon.mode.value

    def shutdown(self) -> None:
        """Close the window, releasing any project it holds."""
        self.window.close()
        self.process_events()


def build_main_window(
    *, project_name: str | None = None, exam_name: str | None = None
) -> ShellHarness:
    """Build the real application window, optionally with a project open.

    Args:
        project_name: Folder name for a throwaway project to create and open.
            ``None`` leaves the window with no project, which is the state the
            footer's "No project open" text has to be captured in.
        exam_name: The examination's display title - what the footer shows.
            Defaults to ``project_name``.

    The window is constructed exactly as `run_gui` constructs it, application
    stylesheet included, because the chrome row's appearance *is* what these
    captures are evidence of. Nothing here exists only for the capture.
    """
    from omr_scanner.config import AppConfig
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.gui.theme import application_stylesheet

    app = ensure_application()
    app.setStyleSheet(application_stylesheet())

    window = MainWindow(config=AppConfig(), config_path=_throwaway_config_path())
    window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    window.show()

    harness = ShellHarness(window=window)
    harness.process_events()

    if project_name is None:
        return harness

    root = OUTPUT_ROOT / f"shell_projects_{uuid_hex()}"
    root.mkdir(parents=True, exist_ok=True)
    if not window.create_project_at(root, project_name, exam_name=exam_name):
        raise RuntimeError(f"Could not create the capture project '{project_name}'")
    harness = ShellHarness(window=window, project_root=root / project_name)
    harness.process_events()
    return harness


def _throwaway_config_path() -> Path:
    """A configuration file under ``test-output/``, never the developer's own.

    A capture run changes application preferences - opening a project writes
    the recent-projects list - and writing those into the real per-user
    configuration would let a diagnostic script quietly edit the settings of
    the person running it.
    """
    path = OUTPUT_ROOT / "shell_config" / f"{uuid_hex()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
