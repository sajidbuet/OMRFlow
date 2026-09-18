"""The Template Designer workflow page.

Purpose:
    Assemble the toolbar, region list, canvas and properties panel from the
    rest of this package into the page shown for the "Template" workflow
    stage, and be the one place that turns a user action into a
    `state.DesignerState` mutation.

Responsibilities:
    * File actions: new template from image, open, save, save as, with dirty
      tracking and a title that shows an unsaved-changes marker.
    * Wire canvas/region-list/properties-panel signals to `DesignerState`
      mutations, always followed by a full refresh (see `_refresh_all`).
    * Marker detection via `services.marker_detection_service`, and region
      creation via the dialogs in `dialogs.py`.
    * Undo/redo, keyboard shortcuts, and the validation dialog.

What does NOT belong here:
    * Any `cv2`/`numpy` import - only `omr_scanner.services` and
      `omr_scanner.domain` types cross into this module.

Shortcut note:
    The main window already binds Ctrl+N/Ctrl+O to *project* actions with a
    window-wide shortcut context, so this page cannot reuse them for
    *template* actions without Qt reporting an ambiguous shortcut (neither
    action would then fire). Template new/open use Ctrl+Shift+N/O instead;
    every other shortcut in the Phase 2 brief (save, undo/redo, delete,
    duplicate, zoom) is free at the window level and is bound as specified.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    IgnoredFieldDefinition,
    MarkerRole,
    OrientationMarker,
    QuestionBlockFieldDefinition,
    RegistrationMarker,
    Zone,
)
from omr_scanner.domain.template_authoring import (
    DEFAULT_BUBBLE_RADIUS,
    DesignerValidationReport,
    apply_default_bubble_radius,
    build_blank_template,
    distribute_columns_evenly,
    measure_column_gap,
    set_zone_bubble_size,
    validate_template_for_designer,
    zone_inherits_bubble_size,
)
from omr_scanner.errors import ImageValidationError, TemplateError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.pages.catalog import WorkflowPageSpec
from omr_scanner.gui.template_designer.canvas import BubbleDotSpec, RegionSpec, TemplateCanvasView
from omr_scanner.gui.template_designer.dialogs import (
    FALLBACK_IMAGE_WIDTH,
    CreateColumnArrayDialog,
    CustomBubbleDialog,
    IgnoredRegionDialog,
    NewTemplateDialog,
    QuestionBlockDialog,
    QuestionSetDialog,
    RegionDialogBase,
    StudentIdDialog,
    ValidationReportDialog,
    _existing_ids,
)
from omr_scanner.gui.template_designer.items import HandleState
from omr_scanner.gui.template_designer.properties_panel import PropertiesPanel
from omr_scanner.gui.template_designer.region_list import RegionListEntry, RegionListPanel
from omr_scanner.gui.template_designer.state import (
    MANUAL_CONFIRMED,
    MISSING,
    DesignerState,
    DetectionMethod,
    MarkerStatus,
)
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET
from omr_scanner.services import (
    DecodedImage,
    MarkerSearchConfig,
    OrientationSearchConfig,
    ProjectSession,
    decode_image_file,
    detect_orientation_marker_in_region,
    detect_registration_markers,
    load_template,
    save_template,
)

MARKER_ITEM_PREFIX = "marker:"
ORIENTATION_ITEM_ID = "orientation"
"""``:`` can never appear in a zone id (`Zone.id` is restricted to
``[A-Za-z0-9_.-]+``), so these ids can never collide with a user's zone id."""

DEFAULT_GRID_SIZE_NORMALIZED = 0.05

TOOLBAR_ICON_SIZE_PX = 20
"""Side length of a toolbar icon, in logical pixels. Qt scales the underlying
SVG for the display's actual DPI, so one value serves 100/125/150% Windows
scaling alike - see `docs/testing/ui_polish_manual_test.md`."""

MIN_BUBBLE_RADIUS_PX = 1.0
MAX_BUBBLE_RADIUS_PX = 200.0
"""Bounds of the bubble radius spin box, in reference-image pixels. Wide enough
for both a 150 dpi scan (bubbles a few pixels across) and a 600 dpi one, and
narrow enough that a mistyped value cannot produce a grid the domain model has to
reject."""

ORIENTATION_DEBUG_ENV = "OMRFLOW_ORIENTATION_DEBUG_DIR"
"""Environment variable naming a directory for the orientation detector's
diagnostic overlay. Unset in an ordinary session, so nothing is written; a
calibration session sets it and gets
``<dir>/orientation_detection_latest.png`` after every attempt. An environment
variable rather than a developer-mode menu because there is nothing to discover,
nothing to leave switched on by accident, and no UI to maintain."""

_RADIUS_EPSILON = 1e-9
"""Below this the spin box's value and the document's stored radius are the same
number, and re-applying it would push a pointless undo entry."""

ORIENTATION_SEARCH_MARGIN = 1.6
"""How much larger than the orientation region's current rectangle the automatic
search area is. The rectangle a user drags around a mark tends to sit slightly
off it; searching a little wider costs nothing (the ROI is still tiny next to the
page) and means a mark just outside the drawn box is still found rather than
reported missing."""


def _marker_item_id(role: MarkerRole) -> str:
    return f"{MARKER_ITEM_PREFIX}{role.value}"


def _role_from_marker_item_id(item_id: str) -> MarkerRole:
    return MarkerRole(item_id.removeprefix(MARKER_ITEM_PREFIX))


class TemplateDesignerPage(WorkflowPage):
    """The interactive template designer.

    Args:
        spec: The "template" workflow stage description.
        parent: Optional Qt parent.
    """

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        # `show_summary=False` / `compact=True`: this page's body is a full-size
        # editor, so the base class's explanatory paragraph and generous margins
        # would spend roughly a sixth of the window's height above the canvas. The
        # sentence survives as the title's tooltip and status tip.
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self._session: ProjectSession | None = None
        self._designer_state: DesignerState | None = None
        self._decoded_image: DecodedImage | None = None
        self._fine_tune_zone_id: str | None = None
        self._pending_dialog_kind: str | None = None

        self._build_toolbar()
        self._build_main_area()
        self._build_status_row()
        self._install_shortcuts()
        self._set_document_controls_enabled(False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _add_toolbar_action(
        self,
        *,
        icon_name: str,
        text: str,
        tooltip: str,
        shortcut_hint: str | None = None,
        callback: Callable[..., object],
        checkable: bool = False,
        icon_only: bool = False,
        toolbar: QToolBar | None = None,
    ) -> QAction:
        """Create, wire and add one toolbar action - the toolbar's action factory.

        Centralising construction here is what keeps every action's icon,
        tooltip and status tip consistent without repeating the same five
        lines of `QAction` setup per button (§19 of the UI polish brief).

        Args:
            icon_name: A bundled Lucide icon name (see `gui.icons.load_icon`).
            text: Label shown beside the icon, or used as the accessible name
                and tooltip title when ``icon_only`` is set.
            tooltip: One-sentence description of what the action does.
            shortcut_hint: Display text for an existing keyboard shortcut
                (e.g. ``"Ctrl+S"``), appended to the tooltip. This does *not*
                register the shortcut - every shortcut here is already bound
                by a `QShortcut` in `_install_shortcuts`, and binding the same
                sequence a second time via `QAction.setShortcut` would make
                Qt refuse both as "ambiguous". The hint is display-only.
            callback: Connected to `toggled` when ``checkable``, else
                `triggered`.
            checkable: Whether this is a toggle action (Grid, Edit Bubbles).
            icon_only: Show only the icon on the toolbar button (with `text`
                still set as the tooltip title and accessible name), for the
                "universal" commands per §5 of the brief; otherwise icon and
                text both show, for the OMR-specific actions whose meaning an
                icon alone would not make obvious.
            toolbar: Which of the two rows to add the action to; defaults to
                row 1 (:attr:`toolbar`).

        Returns:
            The created, already-added `QAction`.
        """
        target = toolbar if toolbar is not None else self.toolbar
        action = QAction(load_icon(icon_name), text, self)
        action.setObjectName(f"action_{_object_name_of(text)}")
        action.setCheckable(checkable)
        full_tooltip = f"{tooltip} ({shortcut_hint})" if shortcut_hint else tooltip
        action.setToolTip(full_tooltip)
        action.setStatusTip(tooltip)
        (action.toggled if checkable else action.triggered).connect(callback)
        target.addAction(action)
        if icon_only:
            button = target.widgetForAction(action)
            if isinstance(button, QToolButton):
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        return action

    def toolbar_actions(self) -> list[QAction]:
        """Every action on either toolbar row, row 1 first.

        The page's toolbar is two rows (see :meth:`_build_toolbar`), so "is this
        action on the toolbar?" is a question about both of them. Exposed as a
        method rather than leaving callers to concatenate
        ``toolbar.actions() + toolbar_view.actions()`` themselves, so adding a
        third row later would not silently make such a check incomplete.
        """
        return [*self.toolbar.actions(), *self.toolbar_view.actions()]

    def _build_toolbar(self) -> None:
        """Build the two toolbar rows.

        Two rows rather than one because a single row holding this many controls
        pushes most of them into Qt's overflow ("»") menu on any window narrower
        than about 1400 px, which hides exactly the region tools a designer uses
        constantly. Splitting them by *what the user is doing* - row 1 manages the
        document, row 2 draws and looks at it - means each row fits at ordinary
        window widths, and neither depends on the other's length.

        Both rows are ordinary `QToolBar`s in an ordinary `QVBoxLayout`: no fixed
        positioning anywhere, so Qt's own overflow handling, label eliding and DPI
        scaling keep working at 100/125/150% Windows scaling alike.
        """
        rows = QVBoxLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)

        self.toolbar = self._make_toolbar_row("Template tools", "template_toolbar_file")
        self.toolbar_view = self._make_toolbar_row(
            "Region and view tools", "template_toolbar_regions"
        )
        rows.addWidget(self.toolbar)
        rows.addWidget(self.toolbar_view)

        # ==== Row 1: file / editing / detection / validation ==============
        # -- File --------------------------------------------------------
        self.new_action = self._add_toolbar_action(
            icon_name="file-plus", text="New", icon_only=True,
            tooltip="Create a new template from a reference image",
            shortcut_hint="Ctrl+Shift+N", callback=self.new_template_from_image,
        )
        self.open_action = self._add_toolbar_action(
            icon_name="folder-open", text="Open", icon_only=True,
            tooltip="Open an existing template",
            shortcut_hint="Ctrl+Shift+O", callback=self.open_template,
        )
        self.save_action = self._add_toolbar_action(
            icon_name="save", text="Save", icon_only=True,
            tooltip="Save the template", shortcut_hint="Ctrl+S", callback=self.save,
        )
        self.save_as_action = self._add_toolbar_action(
            icon_name="save-all", text="Save As", icon_only=True,
            tooltip="Save the template to a new file",
            shortcut_hint="Ctrl+Shift+S", callback=self.save_as,
        )
        self.toolbar.addSeparator()

        # -- Undo / redo ---------------------------------------------------
        self.undo_action = self._add_toolbar_action(
            icon_name="undo-2", text="Undo", icon_only=True,
            tooltip="Undo the last change", shortcut_hint="Ctrl+Z", callback=self.undo,
        )
        self.redo_action = self._add_toolbar_action(
            icon_name="redo-2", text="Redo", icon_only=True,
            tooltip="Redo the last undone change", shortcut_hint="Ctrl+Y", callback=self.redo,
        )
        self.toolbar.addSeparator()

        # -- Registration markers -------------------------------------------
        self.detect_action = self._add_toolbar_action(
            icon_name="scan-line", text="Detect",
            tooltip="Detect the four corner registration markers",
            callback=self.detect_markers,
        )
        self.confirm_markers_action = self._add_toolbar_action(
            icon_name="check-check", text="Confirm",
            tooltip="Accept every automatically detected marker",
            callback=self.confirm_detected_markers,
        )
        self.detect_orientation_action = self._add_toolbar_action(
            icon_name="scan", text="Orientation",
            tooltip=(
                "Find the printed orientation mark inside the orientation "
                "region's current rectangle"
            ),
            callback=self.detect_orientation_marker,
        )
        self.toolbar.addSeparator()

        # -- Validation -------------------------------------------------------
        self.validate_action = self._add_toolbar_action(
            icon_name="circle-check", text="Validate",
            tooltip="Check the current template for errors and warnings",
            callback=self.show_validation,
        )
        self.toolbar.addWidget(_expanding_spacer())

        # ==== Row 2: region tools / bubble geometry / view =================
        # -- Add region ------------------------------------------------------
        self.add_student_id_action = self._add_toolbar_action(
            icon_name="id-card", text="Student ID", toolbar=self.toolbar_view,
            tooltip="Add a student ID bubble region",
            callback=lambda _checked=False: self._start_add_region("student_id"),
        )
        self.add_question_set_action = self._add_toolbar_action(
            icon_name="list-checks", text="Set", toolbar=self.toolbar_view,
            tooltip="Add the question-paper set selection region",
            callback=lambda _checked=False: self._start_add_region("question_set"),
        )
        self.add_question_block_action = self._add_toolbar_action(
            icon_name="circle-dot", text="Questions", toolbar=self.toolbar_view,
            tooltip="Add a block of question-answer bubble regions",
            callback=lambda _checked=False: self._start_add_region("question_block"),
        )
        self.create_array_action = self._add_toolbar_action(
            icon_name="copy-plus", text="Array", toolbar=self.toolbar_view,
            tooltip="Create a repeated array of question columns from the selected column",
            callback=self._on_create_array_requested,
        )
        self.distribute_columns_action = self._add_toolbar_action(
            icon_name="align-horizontal-distribute-center", text="Distribute",
            toolbar=self.toolbar_view,
            tooltip="Space the selected question column's siblings evenly between first and last",
            callback=self._on_distribute_columns_requested,
        )
        self.add_custom_action = self._add_toolbar_action(
            icon_name="square-dashed", text="Custom", toolbar=self.toolbar_view,
            tooltip="Add a custom bubble region",
            callback=lambda _checked=False: self._start_add_region("custom"),
        )
        self.add_ignored_action = self._add_toolbar_action(
            icon_name="file-text", text="Reference", toolbar=self.toolbar_view,
            tooltip="Add a reference region excluded from recognition (a logo or instructions)",
            callback=lambda _checked=False: self._start_add_region("ignored"),
        )
        self.toolbar_view.addSeparator()

        # -- Bubble editing ---------------------------------------------------
        self.fine_tune_action = self._add_toolbar_action(
            icon_name="pencil", text="Edit Bubbles", checkable=True,
            toolbar=self.toolbar_view,
            tooltip="Enable individual bubble position editing for the selected region",
            callback=self._on_fine_tune_toggled,
        )
        self.clear_overrides_action = self._add_toolbar_action(
            icon_name="rotate-ccw", text="Reset", toolbar=self.toolbar_view,
            tooltip="Reset every fine-tuned bubble in the selected region",
            callback=self._clear_selected_zone_overrides,
        )
        self.toolbar_view.addSeparator()

        # -- Bubble geometry ---------------------------------------------------
        self._build_bubble_radius_control()
        self.toolbar_view.addWidget(_expanding_spacer())

        # -- View -----------------------------------------------------------
        # `self.canvas` does not exist yet - `_build_main_area()` runs after
        # this method - so these connect through a lambda that looks it up
        # lazily when clicked, not a bound method that would resolve it (and
        # raise) right now.
        self.zoom_out_action = self._add_toolbar_action(
            icon_name="zoom-out", text="Zoom Out", icon_only=True,
            toolbar=self.toolbar_view,
            tooltip="Zoom out", shortcut_hint="Ctrl+-",
            callback=lambda _checked=False: self.canvas.zoom_out(),
        )
        self.zoom_in_action = self._add_toolbar_action(
            icon_name="zoom-in", text="Zoom In", icon_only=True,
            toolbar=self.toolbar_view,
            tooltip="Zoom in", shortcut_hint="Ctrl++",
            callback=lambda _checked=False: self.canvas.zoom_in(),
        )
        self.fit_action = self._add_toolbar_action(
            icon_name="maximize", text="Fit to Window", icon_only=True,
            toolbar=self.toolbar_view,
            tooltip="Fit the entire sheet in the canvas", shortcut_hint="Ctrl+0",
            callback=lambda _checked=False: self.canvas.fit_to_window(),
        )
        self.actual_size_action = self._add_toolbar_action(
            icon_name="scan-line", text="Actual Size", icon_only=True,
            toolbar=self.toolbar_view,
            tooltip="Display the reference image at 100%",
            callback=lambda _checked=False: self.canvas.zoom_to_actual_size(),
        )
        self.grid_action = self._add_toolbar_action(
            icon_name="grid-3x3", text="Grid", icon_only=True, checkable=True,
            toolbar=self.toolbar_view,
            tooltip="Show or hide the alignment grid", callback=self._on_grid_toggled,
        )

        self.body.addLayout(rows)
        self.create_array_action.setEnabled(False)
        self.distribute_columns_action.setEnabled(False)

        self._document_controls: list[QAction] = [
            self.save_action, self.save_as_action, self.detect_action,
            self.confirm_markers_action, self.detect_orientation_action,
            self.add_student_id_action,
            self.add_question_set_action, self.add_question_block_action,
            self.add_custom_action, self.add_ignored_action, self.fine_tune_action,
            self.clear_overrides_action, self.validate_action,
        ]

    def _make_toolbar_row(self, title: str, object_name: str) -> QToolBar:
        """Construct one toolbar row with the page's shared styling.

        `QToolBar` lays out its own overflow ("»") when the window is too narrow
        for every action, instead of shrinking labels until they clip - the actual
        root cause of the originally reported clipped-text toolbar (a plain
        `QHBoxLayout` of full-text `QPushButton`s has no such mechanism and simply
        compresses every button below its own text width once the row no longer
        fits).
        """
        toolbar = QToolBar(title)
        toolbar.setObjectName(object_name)
        toolbar.setIconSize(QSize(TOOLBAR_ICON_SIZE_PX, TOOLBAR_ICON_SIZE_PX))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)
        return toolbar

    def _build_bubble_radius_control(self) -> None:
        """Add the template-wide bubble radius spin box to toolbar row 2.

        On the toolbar rather than buried in a dialog because it is a *calibration*
        control: the user changes it, looks at the canvas, changes it again. A
        value that needs a dialog opened and accepted for every trial cannot be
        used that way.

        The value is in **reference-image pixels**, matching the properties panel
        and every dialog - never display pixels, so zooming the canvas never
        changes what the number means.
        """
        label = QLabel(" Bubble radius: ")
        label.setObjectName("bubbleRadiusLabel")
        self.toolbar_view.addWidget(label)

        self.bubble_radius_box = QDoubleSpinBox()
        self.bubble_radius_box.setObjectName("bubble_radius")
        self.bubble_radius_box.setDecimals(1)
        self.bubble_radius_box.setRange(MIN_BUBBLE_RADIUS_PX, MAX_BUBBLE_RADIUS_PX)
        self.bubble_radius_box.setSingleStep(1.0)
        self.bubble_radius_box.setSuffix(" px")
        self.bubble_radius_box.setKeyboardTracking(False)
        self.bubble_radius_box.setToolTip(
            "Default bubble radius, in reference-image pixels, for regions "
            "generated in this template. Regions using the default are resized "
            "around their existing centres; a region given its own radius keeps it."
        )
        self.bubble_radius_box.setStatusTip("Template default bubble radius")
        self.bubble_radius_box.valueChanged.connect(self._on_bubble_radius_changed)
        self.toolbar_view.addWidget(self.bubble_radius_box)

    def _build_main_area(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.region_list = RegionListPanel()
        self.region_list.selection_requested.connect(self._on_list_selection_requested)
        self.region_list.visibility_toggled.connect(self._on_visibility_toggled)
        self.region_list.delete_requested.connect(self._on_delete_requested)
        self.region_list.duplicate_requested.connect(self._on_duplicate_requested)
        self.region_list.rename_requested.connect(self._on_rename_requested)
        splitter.addWidget(self.region_list)

        canvas_container = QWidget()
        canvas_layout = QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)

        self.canvas = TemplateCanvasView()
        self.canvas.selection_changed.connect(self._on_canvas_selection_changed)
        self.canvas.geometry_changed.connect(self._on_canvas_geometry_changed)
        self.canvas.geometry_committed.connect(self._on_canvas_geometry_committed)
        self.canvas.region_drawn.connect(self._on_region_drawn)
        self.canvas.bubble_moved.connect(self._on_bubble_moved)
        self.canvas.cursor_moved.connect(self._on_cursor_moved)
        self.canvas.zoom_changed.connect(self._on_zoom_changed)
        canvas_layout.addWidget(self.canvas, stretch=1)
        splitter.addWidget(canvas_container)

        properties_container = QWidget()
        properties_layout = QVBoxLayout(properties_container)
        properties_layout.setContentsMargins(0, 0, 0, 0)
        self.properties = PropertiesPanel()
        self.properties.geometry_edited.connect(self._on_properties_edited)
        self.properties.bubble_radius_edited.connect(self._on_region_bubble_radius_edited)
        self.properties.bubble_inherit_toggled.connect(
            self._on_region_bubble_inherit_toggled
        )
        properties_layout.addWidget(self.properties)
        splitter.addWidget(properties_container)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 900, 260])

        self.body.addWidget(splitter, stretch=1)

    def _build_status_row(self) -> None:
        row = QHBoxLayout()
        self.title_label = QLabel("No template open")
        self.zoom_label = QLabel("Zoom: -")
        self.image_size_label = QLabel("Image: -")
        self.cursor_label = QLabel("Cursor: -")
        self.state_label = QLabel("")
        for label in (
            self.title_label, self.zoom_label, self.image_size_label,
            self.cursor_label, self.state_label,
        ):
            row.addWidget(label)
            row.addSpacing(12)
        row.addStretch(1)
        self.body.addLayout(row)

    def _install_shortcuts(self) -> None:
        bindings: list[tuple[str | QKeySequence.StandardKey, Callable[[], object]]] = [
            ("Ctrl+Shift+N", self.new_template_from_image),
            ("Ctrl+Shift+O", self.open_template),
            (QKeySequence.StandardKey.Save, self.save),
            (QKeySequence.StandardKey.SaveAs, self.save_as),
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
            (QKeySequence.StandardKey.Delete, self._delete_selected),
            ("Ctrl+D", self._duplicate_selected),
            (QKeySequence.StandardKey.ZoomIn, self.canvas.zoom_in),
            (QKeySequence.StandardKey.ZoomOut, self.canvas.zoom_out),
            ("Ctrl+0", self.canvas.fit_to_window),
        ]
        self._shortcuts: list[QShortcut] = []
        for sequence, slot in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    # ------------------------------------------------------------------
    # WorkflowPage hook
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Track the open project, used only to default file dialogs sensibly."""
        self._session = session

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------
    def new_template_from_image(self) -> None:
        """File > New Template from Image."""
        if not self._confirm_discard_changes():
            return
        dialog = NewTemplateDialog(self, initial_directory=self._default_browse_dir())
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.image_path is None:
            return

        try:
            decoded = decode_image_file(dialog.image_path)
        except ImageValidationError as exc:
            report_error(self, exc, context="Load reference image")
            return

        width_mm, height_mm = dialog.page_size_mm()
        template = build_blank_template(
            name=dialog.template_name(),
            canonical_width_px=decoded.width,
            canonical_height_px=decoded.height,
            width_mm=width_mm,
            height_mm=height_mm,
        )
        self._designer_state = DesignerState(
            template, template_path=None, reference_image_path=dialog.image_path
        )
        self._designer_state.marker_status = dict.fromkeys(MarkerRole, MISSING)
        self._designer_state.orientation_status = MISSING
        self._decoded_image = decoded
        self.canvas.set_reference_image(decoded)
        self.properties.set_image_size(decoded.width, decoded.height)
        self.canvas.set_grid_size(DEFAULT_GRID_SIZE_NORMALIZED)
        self._set_document_controls_enabled(True)
        self._refresh_all()

    def open_template(self) -> None:
        """File > Open Template."""
        if not self._confirm_discard_changes():
            return
        start_dir = self._default_browse_dir()
        path_str, _selected_filter = QFileDialog.getOpenFileName(
            self, "Open template", str(start_dir), "OMRFlow templates (*.omrt)"
        )
        if not path_str:
            return
        self._load_template_file(Path(path_str))

    def open_template_at(self, path: Path) -> bool:
        """Open ``path`` directly, without a file dialog.

        The "Edit Template" shortcut another workflow stage (Phase 4
        calibration) offers.

        Returns:
            ``True`` when ``path`` is now the open document. Unsaved changes
            are confirmed exactly as :meth:`open_template` confirms them.
        """
        if not self._confirm_discard_changes():
            return False
        self._load_template_file(path)
        return (
            self._designer_state is not None and self._designer_state.template_path == path
        )

    def _load_template_file(self, path: Path) -> None:
        try:
            template = load_template(path)
        except TemplateError as exc:
            report_error(self, exc, context="Open template")
            return

        reference_path: Path | None = None
        if template.reference_image:
            candidate = (path.parent / template.reference_image).resolve()
            if candidate.is_file():
                reference_path = candidate

        self._designer_state = DesignerState(
            template, template_path=path, reference_image_path=reference_path
        )

        if reference_path is not None:
            try:
                self._decoded_image = decode_image_file(reference_path)
            except ImageValidationError as exc:
                report_error(self, exc, context="Load reference image")
                self._decoded_image = None
        else:
            self._decoded_image = None

        if self._decoded_image is not None:
            self.canvas.set_reference_image(self._decoded_image)
            self.properties.set_image_size(
                self._decoded_image.width, self._decoded_image.height
            )
        else:
            self._show_blank_canvas(
                template.page.canonical_width_px, template.page.canonical_height_px
            )
            self.properties.set_image_size(
                template.page.canonical_width_px, template.page.canonical_height_px
            )

        self.canvas.set_grid_size(DEFAULT_GRID_SIZE_NORMALIZED)
        self._set_document_controls_enabled(True)
        self._refresh_all()

    def save(self) -> bool:
        """File > Save. Returns whether the save actually happened."""
        if self._designer_state is None:
            return False
        if self._designer_state.template_path is None:
            return self.save_as()
        return self._save_to(self._designer_state.template_path)

    def save_as(self) -> bool:
        """File > Save As."""
        if self._designer_state is None:
            return False
        start_dir = self._default_browse_dir()
        suggested = start_dir / f"{_slugify(self._designer_state.template.name)}.omrt"
        path_str, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save template as", str(suggested), "OMRFlow templates (*.omrt)"
        )
        if not path_str:
            return False
        return self._save_to(Path(path_str))

    def _save_to(self, path: Path) -> bool:
        assert self._designer_state is not None
        report = validate_template_for_designer(self._designer_state.template)
        if report.errors:
            proceed = self._confirm_save_with_errors(report)
            if not proceed:
                return False

        to_save = self._designer_state.template.model_copy(
            update={"modified_at": datetime.now(UTC)}
        )
        if self._designer_state.reference_image_path is not None:
            relative = os.path.relpath(
                self._designer_state.reference_image_path, start=path.parent
            )
            to_save = to_save.model_copy(update={"reference_image": relative})

        try:
            written = save_template(to_save, path)
        except TemplateError as exc:
            report_error(self, exc, context="Save template")
            return False

        self._designer_state.mark_saved(written)
        self._update_status()
        return True

    def _confirm_save_with_errors(self, report: DesignerValidationReport) -> bool:
        ValidationReportDialog(report, self).exec()
        choice = QMessageBox.warning(
            self,
            "Template has errors",
            "This template has validation errors. Save it anyway?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
        )
        return choice == QMessageBox.StandardButton.Save

    def _confirm_discard_changes(self) -> bool:
        if self._designer_state is None or not self._designer_state.is_dirty:
            return True
        choice = QMessageBox.warning(
            self,
            "Unsaved changes",
            "The current template has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self.save()
        return True

    def _default_browse_dir(self) -> Path:
        if self._designer_state is not None and self._designer_state.template_path is not None:
            return self._designer_state.template_path.parent
        if self._session is not None:
            return self._session.project.layout.templates_dir
        return Path.home()

    def _show_blank_canvas(self, width: int, height: int) -> None:
        self.canvas.set_blank_canvas(width, height)

    # ------------------------------------------------------------------
    # Undo/redo
    # ------------------------------------------------------------------
    def undo(self) -> None:
        """Edit > Undo."""
        if self._designer_state is None or not self._designer_state.history.can_undo:
            return
        self._designer_state.undo()
        self._refresh_all()

    def redo(self) -> None:
        """Edit > Redo."""
        if self._designer_state is None or not self._designer_state.history.can_redo:
            return
        self._designer_state.redo()
        self._refresh_all()

    # ------------------------------------------------------------------
    # Marker detection
    # ------------------------------------------------------------------
    def detect_markers(self) -> None:
        """Run Phase 1's marker detection against the reference image."""
        if (
            self._designer_state is None
            or self._designer_state.reference_image_path is None
            or self._decoded_image is None
        ):
            QMessageBox.information(
                self,
                "Detect markers",
                "This template has no reference image to detect markers on.",
            )
            return

        try:
            outcome = detect_registration_markers(
                self._designer_state.reference_image_path, config=MarkerSearchConfig()
            )
        except ImageValidationError as exc:
            report_error(self, exc, context="Detect markers")
            return

        for role in MarkerRole:
            detected = outcome.markers.get(role.value)
            if detected is None or not detected.found:
                continue
            nx = detected.x / self._decoded_image.width
            ny = detected.y / self._decoded_image.height
            nw = detected.width / self._decoded_image.width
            nh = detected.height / self._decoded_image.height
            marker = RegistrationMarker(
                role=role,
                center=NormalizedPoint(x=nx, y=ny),
                size=NormalizedSize(width=nw, height=nh),
            )
            self._designer_state.set_marker(
                role,
                marker,
                status=MarkerStatus(
                    confirmed=False, method=DetectionMethod.AUTO, confidence=detected.score
                ),
            )

        missing = [role.value for role in MarkerRole if not outcome.markers[role.value].found]
        self._refresh_all()
        if missing:
            QMessageBox.information(
                self,
                "Detect markers",
                "Marker(s) not found automatically: "
                + ", ".join(missing)
                + ". Drag them into place by hand.",
            )

    def confirm_detected_markers(self) -> None:
        """Mark every auto-detected, unconfirmed marker as accepted."""
        if self._designer_state is None:
            return
        for role in MarkerRole:
            status = self._designer_state.marker_status[role]
            if status.method is DetectionMethod.AUTO and not status.confirmed:
                self._designer_state.confirm_marker(role)
        self._refresh_all()

    def detect_orientation_marker(self) -> None:
        """Find the printed orientation mark inside the orientation region.

        The orientation region's current rectangle *is* the search area: the user
        drags it roughly around the printed mark (or leaves it where a loaded
        template put it) and this replaces it with the mark's measured geometry.
        A mark lying wholly inside that rectangle is exactly what is expected and
        is never rejected for it.

        The search area is widened by :data:`ORIENTATION_SEARCH_MARGIN` before the
        call, because a hand-drawn box usually only approximately contains the
        mark. Everything crossing this boundary is in reference-image pixels; the
        service converts to and from the detector's ROI-local frame.
        """
        if self._designer_state is None or self._decoded_image is None:
            QMessageBox.information(
                self,
                "Detect orientation mark",
                "This template has no reference image to search.",
            )
            return
        reference_path = self._designer_state.reference_image_path
        if reference_path is None:
            QMessageBox.information(
                self,
                "Detect orientation mark",
                "This template has no reference image to search.",
            )
            return

        search = self._orientation_search_region()
        try:
            outcome = detect_orientation_marker_in_region(
                reference_path,
                x=search[0],
                y=search[1],
                width=search[2],
                height=search[3],
                config=OrientationSearchConfig(),
                debug_dir=self._orientation_debug_dir(),
            )
        except (ImageValidationError, ValueError) as exc:
            report_error(self, exc, context="Detect orientation mark")
            return

        if not outcome.found:
            QMessageBox.information(
                self,
                "Detect orientation mark",
                "No orientation mark was found in the orientation region.\n\n"
                f"{outcome.reason}\n\n"
                "Move or enlarge the orientation rectangle over the printed mark "
                "and try again, or drag it into place by hand.",
            )
            return

        image_width, image_height = self._decoded_image.width, self._decoded_image.height
        existing = self._designer_state.template.orientation_marker
        marker = OrientationMarker(
            shape=existing.shape,
            center=NormalizedPoint(
                x=_clamp_unit(outcome.center_x / image_width),
                y=_clamp_unit(outcome.center_y / image_height),
            ),
            size=NormalizedSize(
                width=max(1e-4, outcome.width / image_width),
                height=max(1e-4, outcome.height / image_height),
            ),
            expected_near=existing.expected_near,
            search_radius=existing.search_radius,
        )
        self._designer_state.set_orientation_marker(
            marker,
            status=MarkerStatus(
                confirmed=False, method=DetectionMethod.AUTO, confidence=outcome.score
            ),
        )
        self._refresh_all(keep_selection=ORIENTATION_ITEM_ID)
        self.state_label.setText(
            f"Orientation mark found at ({outcome.center_x:.0f}, {outcome.center_y:.0f}), "
            f"score {outcome.score:.2f}"
        )

    def _orientation_search_region(self) -> tuple[float, float, float, float]:
        """The pixel rectangle the orientation detector searches, widened by margin."""
        assert self._designer_state is not None
        assert self._decoded_image is not None
        image_width, image_height = self._decoded_image.width, self._decoded_image.height
        marker = self._designer_state.template.orientation_marker
        width = marker.size.width * image_width * ORIENTATION_SEARCH_MARGIN
        height = marker.size.height * image_height * ORIENTATION_SEARCH_MARGIN
        return (
            marker.center.x * image_width - width / 2.0,
            marker.center.y * image_height - height / 2.0,
            width,
            height,
        )

    def _orientation_debug_dir(self) -> Path | None:
        """Where the detector should write its diagnostic overlay, if anywhere.

        Off unless :data:`ORIENTATION_DEBUG_ENV` names a directory, so an ordinary
        session writes nothing and a calibration session gets the overlay by
        setting one environment variable - no developer-mode UI to build, discover
        or forget to turn off.
        """
        configured = os.environ.get(ORIENTATION_DEBUG_ENV)
        return Path(configured) if configured else None

    # ------------------------------------------------------------------
    # Region creation
    # ------------------------------------------------------------------
    def _start_add_region(self, kind: str) -> None:
        if self._designer_state is None:
            return
        self._pending_dialog_kind = kind
        self.canvas.start_draw_mode()
        self.state_label.setText("Drag on the canvas to draw the new region...")

    def _on_region_drawn(self, x: float, y: float, width: float, height: float) -> None:
        if (
            self._designer_state is None
            or self._decoded_image is None
            or self._pending_dialog_kind is None
        ):
            return
        image_width, image_height = self._decoded_image.width, self._decoded_image.height
        bounds = NormalizedRect(
            x=max(0.0, min(x / image_width, 1.0)),
            y=max(0.0, min(y / image_height, 1.0)),
            width=min(width / image_width, 1.0 - x / image_width),
            height=min(height / image_height, 1.0 - y / image_height),
        )

        dialog_classes = {
            "student_id": StudentIdDialog,
            "question_set": QuestionSetDialog,
            "question_block": QuestionBlockDialog,
            "custom": CustomBubbleDialog,
            "ignored": IgnoredRegionDialog,
        }
        dialog_cls = dialog_classes.get(self._pending_dialog_kind)
        self._pending_dialog_kind = None
        self.state_label.setText("")
        if dialog_cls is None:
            return

        existing_ids = [zone.id for zone in self._designer_state.template.zones]
        # Every region dialog gets the image size and the template's own default
        # bubble radius, so a new region inherits the template's bubble geometry
        # by default and the user can override it per region in one place.
        common = {
            "bounds": bounds,
            "existing_zone_ids": existing_ids,
            "image_width": image_width,
            "image_height": image_height,
            "bubble_radius_px": self._default_bubble_radius_px(),
            "parent": self,
        }
        dialog: RegionDialogBase
        if dialog_cls is QuestionBlockDialog:
            question_block_dialog = QuestionBlockDialog(**common)  # type: ignore[arg-type]
            question_block_dialog.preview_requested.connect(self._on_question_block_preview)
            dialog = question_block_dialog
        else:
            dialog = dialog_cls(**common)  # type: ignore[arg-type]
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._designer_state.add_zones(dialog.result_zones())
                self._refresh_all()
        finally:
            self.canvas.clear_preview_regions()

    def _on_question_block_preview(self, zones: tuple[Zone, ...]) -> None:
        """Show the question-block dialog's live, not-yet-created preview overlay."""
        if self._decoded_image is None:
            return
        width, height = self._decoded_image.width, self._decoded_image.height
        specs = [
            RegionSpec(
                item_id=f"preview:{zone.id}",
                kind="zone",
                x=zone.bounds.x * width,
                y=zone.bounds.y * height,
                width=zone.bounds.width * width,
                height=zone.bounds.height * height,
                color=zone.display_color,
                label=zone.label,
                bubble_points=_bubble_preview_points(zone, width, height),
                bubble_size=_bubble_pixel_size(zone, width, height),
            )
            for zone in zones
        ]
        self.canvas.show_preview_regions(specs)

    # ------------------------------------------------------------------
    # Question column array / distribute
    # ------------------------------------------------------------------
    def _on_create_array_requested(self) -> None:
        if self._designer_state is None or self._decoded_image is None:
            return
        item_id = self.canvas.selected_region_id()
        reference = self._designer_state.template.zone_by_id(item_id) if item_id else None
        if reference is None or not isinstance(reference.field, QuestionBlockFieldDefinition):
            return
        existing_ids = [
            zone.id for zone in self._designer_state.template.zones if zone.id != reference.id
        ]
        dialog = CreateColumnArrayDialog(
            reference=reference,
            existing_zone_ids=existing_ids,
            image_width=self._decoded_image.width,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_zones = tuple(
            zone for zone in self._designer_state.template.zones if zone.id != reference.id
        ) + dialog.result_zones()
        self._designer_state.apply_zones(new_zones)
        self._refresh_all()

    def _on_distribute_columns_requested(self) -> None:
        if self._designer_state is None:
            return
        item_id = self.canvas.selected_region_id()
        zone = self._designer_state.template.zone_by_id(item_id) if item_id else None
        if zone is None or not isinstance(zone.field, QuestionBlockFieldDefinition):
            return
        siblings = self._group_siblings(zone)
        if len(siblings) < 3:
            return
        distributed = distribute_columns_evenly(siblings)
        distributed_by_id = {result.id: result for result in distributed}
        new_zones = tuple(
            distributed_by_id.get(existing.id, existing)
            for existing in self._designer_state.template.zones
        )
        self._designer_state.apply_zones(new_zones)
        self._refresh_all(keep_selection=zone.id)

    # ------------------------------------------------------------------
    # Canvas <-> state wiring
    # ------------------------------------------------------------------
    def _on_canvas_selection_changed(self, item_id: str | None, kind: str) -> None:
        self.region_list.select(item_id)
        if item_id is None:
            self.properties.clear_selection()
            self._update_question_column_actions(None)
            return
        self._show_properties_for(item_id, kind)

    def _on_canvas_geometry_changed(
        self, item_id: str, kind: str, x: float, y: float, width: float, height: float
    ) -> None:
        title = self._title_for(item_id, kind)
        self.properties.set_geometry(title, x, y, width, height)

    def _on_canvas_geometry_committed(
        self, item_id: str, kind: str, x: float, y: float, width: float, height: float
    ) -> None:
        self._apply_geometry(item_id, kind, x, y, width, height)

    def _on_properties_edited(self, x: float, y: float, width: float, height: float) -> None:
        item_id = self.canvas.selected_region_id()
        if item_id is None:
            return
        kind = "marker" if item_id.startswith(MARKER_ITEM_PREFIX) else (
            "orientation" if item_id == ORIENTATION_ITEM_ID else "zone"
        )
        self._apply_geometry(item_id, kind, x, y, width, height)

    def _apply_geometry(
        self, item_id: str, kind: str, x: float, y: float, width: float, height: float
    ) -> None:
        if self._designer_state is None or self._decoded_image is None:
            return
        image_width, image_height = self._decoded_image.width, self._decoded_image.height
        nx, ny = x / image_width, y / image_height
        nw, nh = width / image_width, height / image_height
        center = NormalizedPoint(
            x=max(0.0, min(nx + nw / 2, 1.0)), y=max(0.0, min(ny + nh / 2, 1.0))
        )
        size = NormalizedSize(width=max(1e-4, nw), height=max(1e-4, nh))

        if kind == "marker":
            role = _role_from_marker_item_id(item_id)
            registration_marker = RegistrationMarker(role=role, center=center, size=size)
            self._designer_state.set_marker(role, registration_marker, status=MANUAL_CONFIRMED)
        elif kind == "orientation":
            existing_orientation = self._designer_state.template.orientation_marker
            orientation_marker = OrientationMarker(
                center=center, size=size, expected_near=existing_orientation.expected_near
            )
            self._designer_state.set_orientation_marker(
                orientation_marker, status=MANUAL_CONFIRMED
            )
        else:
            zone = self._designer_state.template.zone_by_id(item_id)
            if zone is None:
                return
            new_bounds = NormalizedRect(
                x=max(0.0, min(nx, 1.0 - 1e-4)),
                y=max(0.0, min(ny, 1.0 - 1e-4)),
                width=min(nw, 1.0 - max(0.0, nx)),
                height=min(nh, 1.0 - max(0.0, ny)),
            )
            size_changed = (
                abs(new_bounds.width - zone.bounds.width) > 1e-6
                or abs(new_bounds.height - zone.bounds.height) > 1e-6
            )
            if size_changed:
                self._designer_state.resize_zone(item_id, bounds=new_bounds)
            else:
                self._designer_state.move_zone(
                    item_id, dx=new_bounds.x - zone.bounds.x, dy=new_bounds.y - zone.bounds.y
                )

        self._refresh_all(keep_selection=item_id)

    def _show_properties_for(self, item_id: str, kind: str) -> None:
        if self._designer_state is None or self._decoded_image is None:
            return
        title = self._title_for(item_id, kind)
        rect = self._pixel_rect_for(item_id, kind)
        if rect is None:
            self.properties.clear_selection()
            self._update_question_column_actions(None)
            return
        x, y, width, height = rect
        self.properties.set_geometry(title, x, y, width, height)

        zone = self._designer_state.template.zone_by_id(item_id) if kind == "zone" else None
        self._update_question_column_actions(zone)
        self.properties.set_question_column_info(self._question_column_info(zone))
        self._show_bubble_geometry_for(zone)

    def _show_bubble_geometry_for(self, zone: Zone | None) -> None:
        """Populate the properties panel's bubble radius controls for ``zone``."""
        if self._designer_state is None or self._decoded_image is None:
            self.properties.set_bubble_geometry(None, inherits=True)
            return
        if zone is None or zone.grid is None:
            self.properties.set_bubble_geometry(None, inherits=True)
            return
        radius_px = zone.grid.bubble_size.width * self._decoded_image.width / 2.0
        self.properties.set_bubble_geometry(
            radius_px,
            inherits=zone_inherits_bubble_size(
                zone, default=self._designer_state.template.default_bubble_size
            ),
        )

    def _question_column_info(self, zone: Zone | None) -> str | None:
        """Build the properties panel's one-line "which column, how spaced" summary."""
        if (
            self._designer_state is None
            or self._decoded_image is None
            or zone is None
            or not isinstance(zone.field, QuestionBlockFieldDefinition)
        ):
            return None
        siblings = self._group_siblings(zone)
        ordered = sorted(siblings, key=lambda z: z.field.first_question)  # type: ignore[union-attr]
        position = ordered.index(zone) + 1
        info = f"Column {position} of {len(ordered)}"
        if len(ordered) > 1:
            spacing = measure_column_gap(ordered)
            image_width = self._decoded_image.width
            if spacing.uniform and spacing.column_gap is not None:
                info += f" - gap {spacing.column_gap * image_width:.0f}px (uniform)"
            else:
                info += " - gap: custom"
        return info

    def _group_siblings(self, zone: Zone) -> list[Zone]:
        """Every zone sharing ``zone``'s `group_id`, or just ``zone`` if it has none."""
        if self._designer_state is None or not isinstance(zone.field, QuestionBlockFieldDefinition):
            return [zone]
        group_id = zone.field.group_id
        if group_id is None:
            return [zone]
        return [
            candidate
            for candidate in self._designer_state.template.zones
            if isinstance(candidate.field, QuestionBlockFieldDefinition)
            and candidate.field.group_id == group_id
        ]

    def _update_question_column_actions(self, zone: Zone | None) -> None:
        is_question_block = zone is not None and isinstance(
            zone.field, QuestionBlockFieldDefinition
        )
        document_open = self._designer_state is not None
        self.create_array_action.setEnabled(document_open and is_question_block)
        self.distribute_columns_action.setEnabled(document_open and is_question_block)

    def _title_for(self, item_id: str, kind: str) -> str:
        if kind == "marker":
            return f"{_role_from_marker_item_id(item_id).value.replace('_', ' ').title()} marker"
        if kind == "orientation":
            return "Orientation marker"
        if self._designer_state is not None:
            zone = self._designer_state.template.zone_by_id(item_id)
            if zone is not None:
                return zone.label
        return item_id

    def _pixel_rect_for(self, item_id: str, kind: str) -> tuple[float, float, float, float] | None:
        if self._designer_state is None or self._decoded_image is None:
            return None
        width, height = self._decoded_image.width, self._decoded_image.height
        template = self._designer_state.template

        if kind == "marker":
            marker = template.marker_by_role(_role_from_marker_item_id(item_id))
            return (
                (marker.center.x - marker.size.width / 2) * width,
                (marker.center.y - marker.size.height / 2) * height,
                marker.size.width * width,
                marker.size.height * height,
            )
        if kind == "orientation":
            orientation = template.orientation_marker
            return (
                (orientation.center.x - orientation.size.width / 2) * width,
                (orientation.center.y - orientation.size.height / 2) * height,
                orientation.size.width * width,
                orientation.size.height * height,
            )
        zone = template.zone_by_id(item_id)
        if zone is None:
            return None
        return (
            zone.bounds.x * width,
            zone.bounds.y * height,
            zone.bounds.width * width,
            zone.bounds.height * height,
        )

    # ------------------------------------------------------------------
    # Region list wiring
    # ------------------------------------------------------------------
    def _on_list_selection_requested(self, item_id: str) -> None:
        self.canvas.select_region(item_id)

    def _on_visibility_toggled(self, item_id: str, visible: bool) -> None:
        self.canvas.set_region_visible(item_id, visible)

    def _on_delete_requested(self, item_id: str) -> None:
        self._delete_zone(item_id)

    def _on_duplicate_requested(self, item_id: str) -> None:
        self._duplicate_zone(item_id)

    def _on_rename_requested(self, item_id: str, new_label: str) -> None:
        if self._designer_state is None:
            return
        zone = self._designer_state.template.zone_by_id(item_id)
        if zone is None:
            return
        self._designer_state.replace_zone(item_id, zone.model_copy(update={"label": new_label}))
        self._refresh_all(keep_selection=item_id)

    def _delete_selected(self) -> None:
        item_id = self.canvas.selected_region_id()
        if item_id is not None:
            self._delete_zone(item_id)

    def _delete_zone(self, item_id: str) -> None:
        if self._designer_state is None or item_id.startswith(MARKER_ITEM_PREFIX):
            return
        if item_id == ORIENTATION_ITEM_ID:
            return
        if self._designer_state.template.zone_by_id(item_id) is None:
            return
        self._designer_state.remove_zone(item_id)
        self._refresh_all()

    def _duplicate_selected(self) -> None:
        item_id = self.canvas.selected_region_id()
        if item_id is not None:
            self._duplicate_zone(item_id)

    def _duplicate_zone(self, item_id: str) -> None:
        if self._designer_state is None:
            return
        if self._designer_state.template.zone_by_id(item_id) is None:
            return
        existing = [zone.id for zone in self._designer_state.template.zones]
        new_id = _existing_ids(existing, f"{item_id}_copy")
        copy = self._designer_state.duplicate_zone(item_id, new_id=new_id)
        self._refresh_all(keep_selection=copy.id)

    # ------------------------------------------------------------------
    # Fine-tune (individual bubble) mode
    # ------------------------------------------------------------------
    def _on_fine_tune_toggled(self, checked: bool) -> None:
        if not checked:
            self._fine_tune_zone_id = None
            self.canvas.clear_bubble_dots()
            return
        item_id = self.canvas.selected_region_id()
        zone = (
            self._designer_state.template.zone_by_id(item_id)
            if self._designer_state is not None and item_id is not None
            else None
        )
        if zone is None or zone.grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            QMessageBox.information(
                self, "Edit individual bubbles", "Select a bubble region first."
            )
            self.fine_tune_action.setChecked(False)
            return
        self._fine_tune_zone_id = item_id
        self._refresh_bubble_dots()

    def _refresh_bubble_dots(self) -> None:
        if (
            self._designer_state is None
            or self._decoded_image is None
            or self._fine_tune_zone_id is None
        ):
            return
        zone = self._designer_state.template.zone_by_id(self._fine_tune_zone_id)
        if zone is None or zone.grid is None or isinstance(zone.field, IgnoredFieldDefinition):
            return
        width, height = self._decoded_image.width, self._decoded_image.height
        override_cells = {(o.row, o.column) for o in zone.grid.overrides}
        # Half the zone's own bubble width, so a fine-tune dot is the size of the
        # bubble it is moving rather than an arbitrary handle.
        radius = max(2.0, zone.grid.bubble_size.width * width / 2.0)
        dots = []
        for row in range(zone.field.rows):
            for column in range(zone.field.columns):
                center = zone.grid.bubble_center(row, column)
                dots.append(
                    BubbleDotSpec(
                        row=row,
                        column=column,
                        x=center.x * width,
                        y=center.y * height,
                        overridden=(row, column) in override_cells,
                        radius=radius,
                    )
                )
        self.canvas.show_bubble_dots(dots)

    def _on_bubble_moved(self, row: int, column: int, x: float, y: float) -> None:
        if (
            self._designer_state is None
            or self._decoded_image is None
            or self._fine_tune_zone_id is None
        ):
            return
        nx = x / self._decoded_image.width
        ny = y / self._decoded_image.height
        self._designer_state.set_bubble_override(
            self._fine_tune_zone_id, row=row, column=column, center_x=nx, center_y=ny
        )
        self._refresh_region_list()
        self._refresh_canvas_regions(keep_selection=self._fine_tune_zone_id)
        self._refresh_bubble_dots()
        self._update_status()

    def _clear_selected_zone_overrides(self) -> None:
        item_id = self.canvas.selected_region_id() or self._fine_tune_zone_id
        if self._designer_state is None or item_id is None:
            return
        zone = self._designer_state.template.zone_by_id(item_id)
        if zone is None or zone.grid is None:
            return
        for override in zone.grid.overrides:
            self._designer_state.clear_bubble_override(
                item_id, row=override.row, column=override.column
            )
        self._refresh_all(keep_selection=item_id)
        if self._fine_tune_zone_id == item_id:
            self._refresh_bubble_dots()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def show_validation(self) -> None:
        """Tools > Validate Template."""
        if self._designer_state is None:
            return
        report = validate_template_for_designer(self._designer_state.template)
        ValidationReportDialog(report, self).exec()

    # ------------------------------------------------------------------
    # View controls
    # ------------------------------------------------------------------
    def _on_grid_toggled(self, checked: bool) -> None:
        self.canvas.set_grid_visible(checked)

    # ------------------------------------------------------------------
    # Bubble geometry
    # ------------------------------------------------------------------
    def _on_bubble_radius_changed(self, radius_px: float) -> None:
        """Apply a new template-default bubble radius, in image pixels.

        One `DesignerState` mutation, therefore one undo step for the whole
        change however many regions inherit it. Bubble centres and region
        rectangles are preserved by
        :func:`~omr_scanner.domain.template_authoring.apply_default_bubble_radius`;
        a region that carries its own radius is left alone.
        """
        if self._designer_state is None or self._decoded_image is None:
            return
        normalized = radius_px / self._decoded_image.width
        template = self._designer_state.template
        if (
            template.default_bubble_radius is not None
            and abs(template.default_bubble_radius - normalized) <= _RADIUS_EPSILON
        ):
            return
        try:
            updated = apply_default_bubble_radius(template, radius=normalized)
        except (ValueError, ValidationError) as exc:
            report_error(self, exc, context="Set bubble radius")
            self._sync_bubble_radius_box()
            return
        self._designer_state.apply_template(updated)
        self._refresh_all(keep_selection=self.canvas.selected_region_id())

    def _on_region_bubble_radius_edited(self, radius_px: float) -> None:
        """Apply a radius to the selected region only, leaving every other alone."""
        if self._designer_state is None or self._decoded_image is None:
            return
        item_id = self.canvas.selected_region_id()
        zone = (
            self._designer_state.template.zone_by_id(item_id) if item_id is not None else None
        )
        if zone is None or zone.grid is None:
            return
        size = _bubble_size_for_radius(
            radius_px,
            image_width=self._decoded_image.width,
            image_height=self._decoded_image.height,
        )
        try:
            updated = set_zone_bubble_size(zone, bubble_size=size)
        except ValidationError as exc:
            report_error(self, exc, context="Set region bubble radius")
            return
        self._designer_state.replace_zone(zone.id, updated)
        self._refresh_all(keep_selection=zone.id)

    def _on_region_bubble_inherit_toggled(self, inherit: bool) -> None:
        """Return the selected region to the template default, or leave it detached.

        Unchecking does nothing to the geometry - the region already holds its own
        size; it only stops :meth:`_on_bubble_radius_changed` from adopting it,
        which is a property of the size itself rather than of a stored flag (see
        :func:`~omr_scanner.domain.template_authoring.zone_inherits_bubble_size`).
        """
        if not inherit or self._designer_state is None or self._decoded_image is None:
            return
        item_id = self.canvas.selected_region_id()
        zone = (
            self._designer_state.template.zone_by_id(item_id) if item_id is not None else None
        )
        if zone is None or zone.grid is None:
            return
        default = self._designer_state.template.default_bubble_size
        try:
            updated = set_zone_bubble_size(zone, bubble_size=default)
        except ValidationError as exc:
            report_error(self, exc, context="Use template bubble size")
            return
        self._designer_state.replace_zone(zone.id, updated)
        self._refresh_all(keep_selection=zone.id)

    def _sync_bubble_radius_box(self) -> None:
        """Show the open document's default radius without re-triggering a change."""
        if self._designer_state is None or self._decoded_image is None:
            self.bubble_radius_box.setEnabled(False)
            return
        radius = self._designer_state.template.default_bubble_radius or DEFAULT_BUBBLE_RADIUS
        self.bubble_radius_box.setEnabled(True)
        self.bubble_radius_box.blockSignals(True)
        self.bubble_radius_box.setValue(radius * self._decoded_image.width)
        self.bubble_radius_box.blockSignals(False)

    def _default_bubble_radius_px(self) -> float:
        """The open document's default bubble radius, in reference-image pixels."""
        if self._designer_state is None or self._decoded_image is None:
            return DEFAULT_BUBBLE_RADIUS * FALLBACK_IMAGE_WIDTH
        radius = self._designer_state.template.default_bubble_radius or DEFAULT_BUBBLE_RADIUS
        return radius * self._decoded_image.width

    def _on_cursor_moved(self, x: float, y: float) -> None:
        if x < 0:
            self.cursor_label.setText("Cursor: -")
        else:
            self.cursor_label.setText(f"Cursor: ({x:.0f}, {y:.0f})")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"Zoom: {zoom * 100:.0f}%")

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def _refresh_all(self, *, keep_selection: str | None = None) -> None:
        """The one refresh path: model -> canvas -> panels.

        Every mutation ends here rather than updating whichever widget the caller
        happened to be thinking about, which is what keeps "what is on screen" and
        "what the document says" provably the same thing. See
        ``docs/development/template_gui_fix_diagnosis.md`` for the update
        direction this implements.
        """
        self._refresh_canvas_regions(keep_selection=keep_selection)
        self._refresh_region_list()
        self._sync_bubble_radius_box()
        if keep_selection is not None:
            self._refresh_selection_properties(keep_selection)
        self._update_status()
        self._update_undo_redo_buttons()

    def _refresh_selection_properties(self, item_id: str) -> None:
        """Re-read the properties panel's fields from the model after a mutation."""
        kind = (
            "marker"
            if item_id.startswith(MARKER_ITEM_PREFIX)
            else ("orientation" if item_id == ORIENTATION_ITEM_ID else "zone")
        )
        self._show_properties_for(item_id, kind)

    def _refresh_canvas_regions(self, *, keep_selection: str | None = None) -> None:
        if self._designer_state is None or self._decoded_image is None:
            return
        template = self._designer_state.template
        width, height = self._decoded_image.width, self._decoded_image.height
        specs: list[RegionSpec] = []

        for role in MarkerRole:
            marker = template.marker_by_role(role)
            status = self._designer_state.marker_status[role]
            specs.append(
                RegionSpec(
                    item_id=_marker_item_id(role),
                    kind="marker",
                    x=(marker.center.x - marker.size.width / 2) * width,
                    y=(marker.center.y - marker.size.height / 2) * height,
                    width=marker.size.width * width,
                    height=marker.size.height * height,
                    color="#1E88E5",
                    label=role.value.replace("_", " ").upper(),
                    state=_handle_state_for(status),
                )
            )

        orientation = template.orientation_marker
        specs.append(
            RegionSpec(
                item_id=ORIENTATION_ITEM_ID,
                kind="orientation",
                x=(orientation.center.x - orientation.size.width / 2) * width,
                y=(orientation.center.y - orientation.size.height / 2) * height,
                width=orientation.size.width * width,
                height=orientation.size.height * height,
                color="#FB8C00",
                label="ORIENT",
                state=_handle_state_for(self._designer_state.orientation_status),
            )
        )

        for zone in template.zones:
            bubble_points = _bubble_preview_points(zone, width, height)
            specs.append(
                RegionSpec(
                    item_id=zone.id,
                    kind="zone",
                    x=zone.bounds.x * width,
                    y=zone.bounds.y * height,
                    width=zone.bounds.width * width,
                    height=zone.bounds.height * height,
                    color=zone.display_color,
                    label=zone.label,
                    bubble_points=bubble_points,
                    bubble_size=_bubble_pixel_size(zone, width, height),
                )
            )

        self.canvas.rebuild_regions(specs)
        if keep_selection is not None:
            self.canvas.select_region(keep_selection)

    def _refresh_region_list(self) -> None:
        if self._designer_state is None:
            return
        template = self._designer_state.template
        entries = [
            RegionListEntry(
                item_id=_marker_item_id(role),
                kind="marker",
                label=f"{role.value.replace('_', ' ').title()} marker",
            )
            for role in MarkerRole
        ]
        entries.append(
            RegionListEntry(item_id=ORIENTATION_ITEM_ID, kind="orientation", label="Orientation")
        )
        entries.extend(
            RegionListEntry(item_id=zone.id, kind="zone", label=zone.label)
            for zone in template.zones
        )
        self.region_list.set_entries(entries)

    def _update_undo_redo_buttons(self) -> None:
        if self._designer_state is None:
            self.undo_action.setEnabled(False)
            self.redo_action.setEnabled(False)
            return
        self.undo_action.setEnabled(self._designer_state.history.can_undo)
        self.redo_action.setEnabled(self._designer_state.history.can_redo)

    def _update_status(self) -> None:
        if self._designer_state is None:
            self.title_label.setText("No template open")
            return
        name = self._designer_state.template.name
        marker = "*" if self._designer_state.is_dirty else ""
        path_note = (
            self._designer_state.template_path.name
            if self._designer_state.template_path
            else "unsaved"
        )
        self.title_label.setText(f"{name} ({path_note}){marker}")
        if self._decoded_image is not None:
            self.image_size_label.setText(
                f"Image: {self._decoded_image.width} x {self._decoded_image.height}"
            )

    def _set_document_controls_enabled(self, enabled: bool) -> None:
        for action in self._document_controls:
            action.setEnabled(enabled)
        # The radius spin box is a document setting like any of the actions
        # above, so it greys out with them rather than showing an editable
        # number with nothing to apply it to.
        self.bubble_radius_box.setEnabled(enabled)


def _handle_state_for(status: MarkerStatus) -> HandleState:
    """Map session marker status onto the canvas's visual state enum."""
    if status.method is DetectionMethod.AUTO and not status.confirmed:
        if status.confidence is None or status.confidence <= 0.0:
            return HandleState.MISSING
        return HandleState.AUTO_DETECTED
    if status.method is DetectionMethod.MANUAL and not status.confirmed:
        return HandleState.MISSING
    return HandleState.NORMAL


def _bubble_pixel_size(
    zone: Zone, width: float, height: float
) -> tuple[float, float] | None:
    """Return a zone's bubble size in image pixels, or ``None`` for one with no grid."""
    if zone.grid is None:
        return None
    return (zone.grid.bubble_size.width * width, zone.grid.bubble_size.height * height)


def _bubble_size_for_radius(
    radius_px: float, *, image_width: float, image_height: float
) -> NormalizedSize:
    """Convert a pixel radius into the normalised bounding size the grid stores.

    ``2 * radius`` on each axis in *pixels*, normalised per axis afterwards - so a
    circle on paper stays a circle, and becomes an ellipse in normalised
    coordinates exactly as the anisotropic normalised page requires.
    """
    return NormalizedSize(
        width=min(1.0, max(1e-6, 2.0 * radius_px / image_width)),
        height=min(1.0, max(1e-6, 2.0 * radius_px / image_height)),
    )


def _clamp_unit(value: float) -> float:
    """Clamp a normalised coordinate into ``[0, 1]``."""
    return max(0.0, min(1.0, value))


def _expanding_spacer() -> QWidget:
    """An invisible widget that pushes whatever follows it to the row's right end."""
    spacer = QWidget()
    spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return spacer


def _object_name_of(text: str) -> str:
    """Turn an action's label into a stable Qt object name for semantic test access."""
    return "".join(char if char.isalnum() else "_" for char in text).strip("_").lower()


def _bubble_preview_points(
    zone: Zone, width: float, height: float
) -> tuple[tuple[float, float], ...]:
    """Compute bubble centre preview points for a zone, in image pixels.

    Capped at a few hundred points: a preview dot a couple of pixels across is
    illegible past that density anyway, and this keeps every zone's `paint()`
    call cheap even for a very large question block.
    """
    if zone.grid is None or isinstance(zone.field, IgnoredFieldDefinition):
        return ()
    rows, columns = zone.field.rows, zone.field.columns
    total = rows * columns
    if total > 800:
        return ()
    points = []
    for row in range(rows):
        for column in range(columns):
            center = zone.grid.bubble_center(row, column)
            points.append((center.x * width, center.y * height))
    return tuple(points)


def _slugify(name: str) -> str:
    """Turn a template name into a filesystem-safe filename stem."""
    safe = "".join(char if char.isalnum() or char in "-_ " else "_" for char in name)
    return safe.strip().replace(" ", "_") or "template"


__all__ = ["TemplateDesignerPage"]
