"""The application's single chrome row.

Purpose:
    Carry, in one 46-pixel line (at 100% interface zoom), everything the shell
    used to spread across a native title bar, a branded header band and a
    workflow navigator band:

        [menu] [OMRFlow] | [-][+] [<] [ 1 Project > ... > 9 Reports ] [>]  _ □ X

    ``[-]`` and ``[+]`` are the *interface zoom* controls: they make the whole
    application - this row included - smaller or larger, and they are the
    same commands as ``View > Zoom -`` and ``View > Zoom +``. They used to
    adjust only the workflow ribbon's density; that adjustment now lives at
    ``View > Ribbon Density``, because a ``-``/``+`` pair in a title bar reads
    as zoom and should be zoom.

    Every pixel saved here is a pixel the Template, Calibrate, Scan, Resolve
    and Reports stages get back, which on a 1366x768 display is the difference
    between a usable workspace and a letterbox.

Responsibilities:
    * :class:`AppChrome` - the row: its controls, its layout, and the
      window-drag behaviour that a removed title bar has to give back.

What does NOT belong here:
    * The application menu's *contents*. The row is handed a `QMenu` the main
      window already built and still owns, so there is exactly one File menu
      in the application.
    * What a navigation, a zoom change or a window-state change *means*.
      Every control emits or triggers an action; the main window decides.
      That is what lets this widget be built and driven in a test with no
      window behind it.
    * The zoom itself. The buttons are bound to the main window's
      ``View > Zoom`` actions (:meth:`AppChrome.bind_zoom_actions`), so the
      menu and the row cannot disagree about what is enabled.
    * The workflow layout algorithm - see
      :mod:`omr_scanner.gui.widgets.workflow_ribbon`.

Why dragging uses ``QWindow.startSystemMove()``:
    Removing the title bar removes Windows' own move handling with it, and
    the tempting replacement - remember the press position, then ``move()``
    the window on every mouse-move - is the version that feels wrong. It
    lags behind the pointer, it defeats Aero Snap entirely (no half-screen
    docking, no snap to the top to maximise), it misbehaves across monitors
    with different DPI, and it keeps dragging if the release event is lost.
    ``startSystemMove()`` hands the drag to the window manager, which is what
    gives all of that back for free and is Qt's own documented answer to this
    exact problem. The same reasoning applies to
    ``QWindow.startSystemResize()``, which the main window uses for the
    frame - see :mod:`omr_scanner.gui.main_window`.

Why a press has to be classified before it can drag:
    A chrome row that dragged the window from anywhere would make every
    control in it unusable. :meth:`AppChrome.is_drag_area` is the rule, and it
    is deliberately a plain query on a point so that a test can assert "the
    logo drags, a workflow step does not" without synthesising a drag that
    the offscreen platform would refuse to start anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, logo_svg_path, logo_view_box
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import Chrome, IconSize, UiScale
from omr_scanner.gui.ui_scale import add_scaled_spacing, current_scale
from omr_scanner.gui.widgets.window_buttons import WindowButton, WindowButtonKind
from omr_scanner.gui.widgets.workflow_ribbon import WorkflowRibbon

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from PySide6.QtGui import QAction, QMouseEvent

    from omr_scanner.gui.widgets.workflow_ribbon import StageSpec

MENU_ACCESSIBLE_NAME = "Application menu"
"""What a screen reader calls the row's menu button.

Spelled out because the button shows an icon and no text: without this a
screen reader would announce it as an unnamed button, which is the one thing
an icon-only control must never be.
"""

LOGO_ACCESSIBLE_NAME = "OMRFlow"

ZOOM_OUT_NAME = "Decrease interface zoom"
ZOOM_IN_NAME = "Increase interface zoom"
PREVIOUS_NAME = "Previous workflow stage"
NEXT_NAME = "Next workflow stage"


class AppChrome(QWidget):
    """The application's one chrome row.

    Args:
        specs: The workflow stages, in order, handed straight to the ribbon.
        density: The ribbon's initial density level.
        menu: The application menu to pop up. Built and owned by the main
            window; the row only shows it.
        parent: Optional Qt parent.

    Signals:
        step_activated: A workflow stage was chosen. Carries its key.
        previous_requested: The ``<`` button was pressed.
        next_requested: The ``>`` button was pressed.
        density_changed: The ribbon's density level changed. Carries the new
            level, for persistence. Relayed from the ribbon, whatever changed
            it - today that is ``View > Ribbon Density``.
        minimise_requested: The minimise button was pressed.
        maximise_toggled: The maximise/restore button was pressed, or a valid
            drag area was double-clicked.
        close_requested: The close button was pressed.

    Attributes:
        menu_button: The hamburger button.
        logo: The wordmark. Also a drag handle.
        ribbon: The :class:`~omr_scanner.gui.widgets.workflow_ribbon.WorkflowRibbon`.
        previous_button, next_button: The workflow arrows.
        zoom_out_button, zoom_in_button: The ``-`` and ``+`` interface zoom
            controls. Inert until :meth:`bind_zoom_actions` hands them the
            main window's actions.
        minimise_button, maximise_button, close_button: The window controls.
    """

    step_activated = Signal(str)
    previous_requested = Signal()
    next_requested = Signal()
    density_changed = Signal(int)
    minimise_requested = Signal()
    maximise_toggled = Signal()
    close_requested = Signal()

    def __init__(
        self,
        specs: Iterable[StageSpec],
        density: int,
        menu: QMenu | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appChrome")
        self._scale = current_scale()
        self.setFixedHeight(self._scale.px(Chrome.HEIGHT))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        self._layout = layout
        # No vertical margin: the window buttons are the row's full height, as
        # a title bar's are, and the shorter controls are centred by the
        # layout rather than by a margin that would cap them.
        layout.setContentsMargins(self._scale.px(Chrome.H_PADDING), 0, 0, 0)
        layout.setSpacing(self._scale.px(Chrome.GROUP_GAP))

        self.menu_button = self._build_menu_button(menu)
        layout.addWidget(self.menu_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.logo = self._build_logo()
        layout.addWidget(self.logo, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.separator = self._build_separator()
        layout.addWidget(self.separator, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.zoom_out_button = self._build_small_button(
            "zoom-out", ZOOM_OUT_NAME, "zoomOutButton"
        )
        self.zoom_out_button.setToolTip(ZOOM_OUT_NAME)
        layout.addWidget(self.zoom_out_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.zoom_in_button = self._build_small_button(
            "zoom-in", ZOOM_IN_NAME, "zoomInButton"
        )
        self.zoom_in_button.setToolTip(ZOOM_IN_NAME)
        layout.addWidget(self.zoom_in_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.previous_button = self._build_small_button(
            "chevron-left", PREVIOUS_NAME, "previousStageButton"
        )
        self.previous_button.setToolTip(PREVIOUS_NAME)
        self.previous_button.clicked.connect(self.previous_requested.emit)
        layout.addWidget(self.previous_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.ribbon = WorkflowRibbon(specs, density=density, parent=self)
        self.ribbon.step_activated.connect(self.step_activated.emit)
        self.ribbon.density_changed.connect(self.density_changed.emit)
        layout.addWidget(self.ribbon, 1, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.next_button = self._build_small_button(
            "chevron-right", NEXT_NAME, "nextStageButton"
        )
        self.next_button.setToolTip(NEXT_NAME)
        self.next_button.clicked.connect(self.next_requested.emit)
        layout.addWidget(self.next_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        # The gap between the workflow arrows and the window controls. It is
        # the row's only guaranteed drag area once the ribbon fills its
        # viewport, which is why it is a named width and not zero.
        add_scaled_spacing(layout, Chrome.DRAG_HANDLE_WIDTH)

        self.minimise_button = WindowButton(WindowButtonKind.MINIMISE, self)
        self.minimise_button.clicked.connect(self.minimise_requested.emit)
        layout.addWidget(self.minimise_button)

        self.maximise_button = WindowButton(WindowButtonKind.MAXIMISE, self)
        self.maximise_button.clicked.connect(self.maximise_toggled.emit)
        layout.addWidget(self.maximise_button)

        self.close_button = WindowButton(WindowButtonKind.CLOSE, self)
        self.close_button.clicked.connect(self.close_requested.emit)
        layout.addWidget(self.close_button)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_menu_button(self, menu: QMenu | None) -> QToolButton:
        """The hamburger.

        Its icon is a bundled Lucide vector and not "☰": a Unicode glyph is a
        *font* dependency, rendering at a different weight and vertical offset
        in every UI font, missing outright from some, and unable to take the
        application's own colour.
        """
        button = QToolButton(self)
        button.setObjectName("appMenuButton")
        button.setIcon(load_icon("menu"))
        extent = self._scale.px(IconSize.CHROME_MENU)
        button.setIconSize(QSize(extent, extent))
        side = self._scale.px(Chrome.MENU_BUTTON_SIZE)
        button.setFixedSize(side, side)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Reachable by Tab, and it is the first thing in the tab order, so the
        # whole menu hierarchy is available from the keyboard without a mouse.
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(MENU_ACCESSIBLE_NAME)
        button.setAccessibleDescription("Open the File, View, Tools and Help menus")
        button.setToolTip("Application menu (File, View, Tools, Help)")
        button.setStatusTip("File, View, Tools and Help")
        if menu is not None:
            button.setMenu(menu)
        return button

    def set_menu(self, menu: QMenu) -> None:
        """Attach the application menu after construction."""
        self.menu_button.setMenu(menu)

    def _build_logo(self) -> QSvgWidget:
        """The wordmark, as a vector and at the artwork's own aspect ratio.

        A `QSvgWidget` and not a `QPixmap`: the logo has to be crisp at 100%,
        125%, 150%, 175% and 200% display scaling, and a raster asset needs
        one file per scale and still blurs between them.

        The renderer's view box is narrowed to the artwork's own bounds first.
        Without that step `QSvgWidget` maps the file's square 1254x1254 canvas
        onto the widget, which both distorts the wordmark - by exactly
        :data:`~omr_scanner.gui.branding.LOGO_ASPECT_RATIO`, since the widget
        is sized to the *ink's* ratio and the canvas is square - and wastes
        roughly a third of the width on transparent padding. With it, the
        widget's rectangle and the ink's rectangle have the same proportions,
        so the mapping is a pure scale.

        Transparent to the mouse on purpose: the brief asks for the logo area
        to be a window-drag handle, and the cleanest way to make a press there
        reach :meth:`mousePressEvent` is for the logo not to be a mouse
        target at all.
        """
        logo = QSvgWidget(str(logo_svg_path()), self)
        logo.setObjectName("appLogo")
        renderer = logo.renderer()
        if renderer is not None:  # pragma: no branch - the asset always loads
            renderer.setViewBox(logo_view_box())
        logo.setFixedSize(self._logo_size())
        logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        logo.setAccessibleName(LOGO_ACCESSIBLE_NAME)
        logo.setToolTip("OMRFlow - drag here to move the window")
        return logo

    def _build_separator(self) -> QFrame:
        separator = QFrame(self)
        separator.setObjectName("appChromeSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedSize(self._separator_size())
        separator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return separator

    def _build_small_button(
        self, icon_name: str, accessible_name: str, object_name: str
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setProperty("chromeControl", True)
        button.setIcon(load_icon(icon_name))
        extent = self._scale.px(IconSize.CHROME_CONTROL)
        button.setIconSize(QSize(extent, extent))
        side = self._scale.px(Chrome.SMALL_BUTTON_SIZE)
        button.setFixedSize(side, side)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(accessible_name)
        button.setStatusTip(accessible_name)
        return button

    def _logo_size(self) -> QSize:
        height = self._scale.px(Chrome.LOGO_HEIGHT)
        return QSize(round(height * LOGO_ASPECT_RATIO), height)

    def _separator_size(self) -> QSize:
        return QSize(
            self._scale.stroke(1),
            self._scale.px(Chrome.HEIGHT) - 2 * self._scale.px(Chrome.V_PADDING),
        )

    # ------------------------------------------------------------------
    # Interface zoom
    # ------------------------------------------------------------------
    def bind_zoom_actions(self, zoom_out: QAction, zoom_in: QAction) -> None:
        """Make ``-`` and ``+`` trigger the main window's zoom actions.

        ``setDefaultAction`` and not a ``clicked`` connection: the button then
        takes its enabled state from the action, so at 200% the ``+`` in this
        row and ``View > Zoom +`` are disabled by the same call, and they
        cannot be out of step. The buttons keep their own icon, size and
        accessible name - the action supplies the behaviour, the tooltip and
        whether it is available.
        """
        for button, action, name, icon in (
            (self.zoom_out_button, zoom_out, ZOOM_OUT_NAME, "zoom-out"),
            (self.zoom_in_button, zoom_in, ZOOM_IN_NAME, "zoom-in"),
        ):
            button.setDefaultAction(action)
            button.setIcon(load_icon(icon))
            button.setAccessibleName(name)
            button.setStatusTip(name)

    def apply_ui_scale(self, scale: UiScale) -> None:
        """Resize the whole row - height, controls, icons, gaps - to ``scale``.

        The ribbon and the window buttons rescale themselves; this sizes
        everything the row owns directly. Nothing is rebuilt: the same
        widgets, with the same state, get new metrics.
        """
        if scale == self._scale:
            return
        self._scale = scale
        self.setFixedHeight(scale.px(Chrome.HEIGHT))
        self._layout.setContentsMargins(scale.px(Chrome.H_PADDING), 0, 0, 0)
        self._layout.setSpacing(scale.px(Chrome.GROUP_GAP))

        menu_side = scale.px(Chrome.MENU_BUTTON_SIZE)
        menu_icon = scale.px(IconSize.CHROME_MENU)
        self.menu_button.setFixedSize(menu_side, menu_side)
        self.menu_button.setIconSize(QSize(menu_icon, menu_icon))

        self.logo.setFixedSize(self._logo_size())
        self.separator.setFixedSize(self._separator_size())

        small_side = scale.px(Chrome.SMALL_BUTTON_SIZE)
        small_icon = scale.px(IconSize.CHROME_CONTROL)
        for button in (
            self.zoom_out_button,
            self.zoom_in_button,
            self.previous_button,
            self.next_button,
        ):
            button.setFixedSize(small_side, small_side)
            button.setIconSize(QSize(small_icon, small_icon))

        for window_button in (self.minimise_button, self.maximise_button, self.close_button):
            window_button.apply_ui_scale(scale)
        self.ribbon.apply_ui_scale(scale)
        self._layout.invalidate()
        self.updateGeometry()

    # ------------------------------------------------------------------
    # Workflow arrows
    # ------------------------------------------------------------------
    def sync_navigation_buttons(self) -> None:
        """Enable the arrows only where there is somewhere to go.

        "Somewhere to go" means an *adjacent* stage that is currently
        permitted - see
        :meth:`~omr_scanner.gui.widgets.workflow_ribbon.WorkflowRibbon.adjacent_key`.
        At stage 1 the previous arrow is therefore disabled, at stage 9 the
        next arrow is, and beside a locked stage the arrow pointing at it is
        too, rather than skipping over it to somewhere the operator did not
        ask for.
        """
        previous_key = self.ribbon.adjacent_key(-1)
        next_key = self.ribbon.adjacent_key(1)
        self.previous_button.setEnabled(previous_key is not None)
        self.next_button.setEnabled(next_key is not None)
        self.previous_button.setToolTip(
            self._arrow_tooltip(PREVIOUS_NAME, previous_key)
        )
        self.next_button.setToolTip(self._arrow_tooltip(NEXT_NAME, next_key))

    def _arrow_tooltip(self, name: str, key: str | None) -> str:
        if key is None:
            return f"{name} (none available)"
        step = self.ribbon.step(key)
        return f"{name}: {step.display_text}" if step is not None else name

    # ------------------------------------------------------------------
    # Window state
    # ------------------------------------------------------------------
    def set_maximised(self, maximised: bool) -> None:
        """Swap the maximise button between its two glyphs and names."""
        self.maximise_button.set_kind(
            WindowButtonKind.RESTORE if maximised else WindowButtonKind.MAXIMISE
        )

    # ------------------------------------------------------------------
    # Dragging the window
    # ------------------------------------------------------------------
    def is_drag_area(self, position: QPoint) -> bool:
        """Whether a press at ``position`` should move the window.

        Args:
            position: A point in this widget's own coordinates.

        Returns:
            ``True`` for the row's empty space and for the logo, ``False``
            over any interactive control - the menu button, the zoom and
            arrow buttons, a workflow step, and the window buttons.

        Implemented as "is there an interactive child here?" rather than as a
        list of draggable rectangles, because the second kind of rule is the
        kind that goes stale: adding a control to this row would silently make
        its area draggable, and the operator would discover it by moving the
        window when they meant to press a button. The logo and the separator
        opt out by being transparent to the mouse, so ``childAt`` skips them
        and they answer ``True`` here.
        """
        return self.childAt(position) is None

    def _start_system_move(self) -> bool:
        """Ask the window manager to take over the drag.

        Returns:
            Whether it accepted. It declines on the offscreen platform used by
            headless tests, and on any platform without a compositor that
            supports it, in which case the press is simply not a drag - the
            window stays where it is rather than being moved badly by hand.
        """
        window = self.window().windowHandle()
        return bool(window is not None and window.startSystemMove())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Begin a native window move from the row's empty space or the logo."""
        if event.button() is Qt.MouseButton.LeftButton and self.is_drag_area(
            event.position().toPoint()
        ) and self._start_system_move():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Toggle maximise, as double-clicking a title bar does.

        Only from a valid drag area, for the same reason a press only starts a
        move from one: double-clicking the ``+`` button twice quickly must
        zoom in twice, not maximise the window.
        """
        if event.button() is Qt.MouseButton.LeftButton and self.is_drag_area(
            event.position().toPoint()
        ):
            self.maximise_toggled.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def minimumSizeHint(self) -> QSize:
        """Everything except the ribbon, which changes layout instead.

        The ribbon's own minimum is two pixels of padding, so what is left
        here is the branding, the five buttons and the three window controls.
        Including a ribbon width would make the workflow dictate how small the
        application can be, and the narrow layout exists precisely so that it
        does not.

        Measured at the current interface zoom, so a larger zoom raises the
        floor by exactly what the row's own controls grew by, and no more.
        """
        px = self._scale.px
        fixed = (
            px(Chrome.H_PADDING)
            + px(Chrome.MENU_BUTTON_SIZE)
            + self.logo.width()
            + self.separator.width()
            + 4 * px(Chrome.SMALL_BUTTON_SIZE)
            + 3 * px(Chrome.WINDOW_BUTTON_WIDTH)
            + px(Chrome.DRAG_HANDLE_WIDTH)
            + 8 * px(Chrome.GROUP_GAP)
        )
        return QSize(fixed, px(Chrome.HEIGHT))

    def sizeHint(self) -> QSize:
        """The minimum plus whatever the ribbon would like."""
        minimum = self.minimumSizeHint()
        return QSize(minimum.width() + self.ribbon.sizeHint().width(), minimum.height())


__all__ = [
    "LOGO_ACCESSIBLE_NAME",
    "MENU_ACCESSIBLE_NAME",
    "NEXT_NAME",
    "PREVIOUS_NAME",
    "ZOOM_IN_NAME",
    "ZOOM_OUT_NAME",
    "AppChrome",
]
