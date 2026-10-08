"""The global interface zoom, applied to a running application.

Purpose:
    Make the operator's interface zoom (``View > Zoom +``, the chrome row's
    zoom buttons) resize the *whole* interface - text, controls, icons,
    margins, the chrome row and the footer - live, without restarting, and
    without disturbing anything the operator is doing.

Responsibilities:
    * :class:`UiScaleManager` - owns the current zoom, the original 100%
      application font, the scaled proxy style and the application
      stylesheet, and refreshes every widget when the zoom changes.
    * A small declarative vocabulary for widgets whose geometry is set in
      Python: :func:`scale_widget`, :func:`scale_layout`,
      :func:`add_scaled_spacing`, :func:`set_relative_font`,
      :func:`set_scaled_stylesheet`. Each records the *canonical* value on the
      widget and applies it at the current zoom; the manager re-applies it at
      every later zoom.
    * :class:`ScaleAware` - the hook for the few custom widgets (the chrome
      row, the ribbon, the footer) whose geometry is arithmetic rather than a
      list of sizes.

What does NOT belong here:
    * The arithmetic of a scale - :class:`omr_scanner.gui.theme.UiScale`.
    * Persisting the preference. :class:`~omr_scanner.config.AppConfig` holds
      it and :class:`~omr_scanner.gui.main_window.MainWindow` saves it; the
      manager is told a percentage and does not know where it came from.
    * Any sheet or image zoom. The template canvas, the calibration and scan
      previews and the Resolve views each keep their own transform, and no
      code here touches a `QGraphicsView`'s transform or a scene coordinate.

How the zoom reaches every kind of widget:
    * **Text** - the application font is set to the platform's original UI
      font times the zoom. The original is captured once per
      `QApplication` and never re-read from the (by then scaled) application
      font, so zooming cannot compound.
    * **Stylesheet geometry** - the application sheet is recomposed at the new
      scale (:func:`~omr_scanner.gui.theme.application_stylesheet`). Because
      it is set on the application, every dialog opened afterwards - Qt's own
      message boxes and file dialogs included - is born at the new zoom.
    * **Qt's own metrics** - default layout margins and spacing, icon sizes,
      check-box indicators, header sections - come from the style. A
      `QProxyStyle` multiplies exactly those metrics, so a dialog that never
      set a margin still scales.
    * **Geometry set in Python** - declared through the helpers below, which
      store the canonical value in a dynamic property and apply it scaled.
    * **Widget-level stylesheets** are re-set after the font changes. Qt does
      not propagate a new application font into a widget styled by a sheet
      until that sheet is re-applied, which is measured, not assumed - see
      ``tests/gui/test_ui_zoom.py``.

Why a dynamic property and not a Python attribute:
    A widget built by Qt (a dialog's button box, a header view) may reach
    Python through a wrapper that is recreated between calls, and a Python
    attribute set on the old wrapper is gone. A Qt property lives on the C++
    object, so the manager's walk over ``QApplication.allWidgets()`` always
    finds it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFormLayout,
    QGridLayout,
    QLayout,
    QProxyStyle,
    QSizePolicy,
    QStyle,
    QStyleFactory,
    QWidget,
)

from omr_scanner.gui.theme.scale import UiScale
from omr_scanner.gui.theme.stylesheet import SCALED_STYLESHEETS, application_stylesheet

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from PySide6.QtWidgets import QStyleOption

logger = logging.getLogger(__name__)

ZOOM_PROPERTY: Final = "omrUiZoomPercent"
"""Dynamic property on the `QApplication` holding the current zoom.

Read by :func:`current_scale`, which paint events call - a property read is
cheap enough for that, a search of the application's children would not be."""

DECLARATION_PROPERTY: Final = "omrUiScale"
"""Dynamic property on a widget or layout holding its canonical metrics."""

MANAGER_OBJECT_NAME: Final = "omrUiScaleManager"

SPACERS_PROPERTY: Final = "omrScaledSpacers"
"""Dynamic property on a box layout: ``[[item index, 100% length], ...]``.

Indices, not `QSpacerItem` references, on purpose. A spacer item is not a
`QObject`, so nothing tells PySide when its layout deletes it; a wrapper kept
alive past that point leaves a stale entry in PySide's address map, and a
widget later allocated at the same address comes back from
`QApplication.allWidgets()` as that stale `QSpacerItem`. Only short-lived
wrappers are ever made here."""

_SCALED_METRICS: Final = frozenset(
    {
        QStyle.PixelMetric.PM_SmallIconSize,
        QStyle.PixelMetric.PM_LargeIconSize,
        QStyle.PixelMetric.PM_ToolBarIconSize,
        QStyle.PixelMetric.PM_ButtonIconSize,
        QStyle.PixelMetric.PM_TabBarIconSize,
        QStyle.PixelMetric.PM_ListViewIconSize,
        QStyle.PixelMetric.PM_IconViewIconSize,
        QStyle.PixelMetric.PM_MessageBoxIconSize,
        QStyle.PixelMetric.PM_IndicatorWidth,
        QStyle.PixelMetric.PM_IndicatorHeight,
        QStyle.PixelMetric.PM_ExclusiveIndicatorWidth,
        QStyle.PixelMetric.PM_ExclusiveIndicatorHeight,
        QStyle.PixelMetric.PM_LayoutLeftMargin,
        QStyle.PixelMetric.PM_LayoutTopMargin,
        QStyle.PixelMetric.PM_LayoutRightMargin,
        QStyle.PixelMetric.PM_LayoutBottomMargin,
        QStyle.PixelMetric.PM_LayoutHorizontalSpacing,
        QStyle.PixelMetric.PM_LayoutVerticalSpacing,
        QStyle.PixelMetric.PM_HeaderDefaultSectionSizeHorizontal,
        QStyle.PixelMetric.PM_HeaderDefaultSectionSizeVertical,
        QStyle.PixelMetric.PM_HeaderMargin,
        QStyle.PixelMetric.PM_MenuButtonIndicator,
        QStyle.PixelMetric.PM_SliderThickness,
        QStyle.PixelMetric.PM_SliderLength,
        QStyle.PixelMetric.PM_SliderControlThickness,
        QStyle.PixelMetric.PM_DialogButtonsButtonWidth,
        QStyle.PixelMetric.PM_DialogButtonsButtonHeight,
        QStyle.PixelMetric.PM_DialogButtonsSeparator,
        QStyle.PixelMetric.PM_ToolBarItemSpacing,
        QStyle.PixelMetric.PM_ToolBarSeparatorExtent,
        QStyle.PixelMetric.PM_ToolBarExtensionExtent,
        QStyle.PixelMetric.PM_CheckBoxLabelSpacing,
        QStyle.PixelMetric.PM_RadioButtonLabelSpacing,
        QStyle.PixelMetric.PM_ToolTipLabelFrameWidth,
        QStyle.PixelMetric.PM_TreeViewIndentation,
    }
)
"""The style metrics that are interface lengths.

Deliberately a list, not "every metric": frame widths and focus-frame margins
are hairlines that should stay hairlines, and scroll bar extents are already
set by the application stylesheet. A metric missing from here is simply drawn
at its 100% size - visible in a screenshot, harmless to behaviour."""


@runtime_checkable
class ScaleAware(Protocol):
    """A widget that recomputes its own geometry for an interface zoom.

    For geometry that is arithmetic rather than a list of sizes - the chrome
    row's minimum width, the ribbon's layout plan, the footer's tiers. Called
    by :class:`UiScaleManager` after the font and stylesheets have changed,
    so an implementation can measure text in the new font. Implementations
    also call it themselves at the end of construction with
    :func:`current_scale`, so a widget built while zoomed starts right.
    """

    def apply_ui_scale(self, scale: UiScale) -> None:
        """Adopt ``scale``."""


# ----------------------------------------------------------------------
# The current scale
# ----------------------------------------------------------------------
def current_scale() -> UiScale:
    """The interface zoom in effect now; 100% with no application."""
    app = QApplication.instance()
    if app is None:
        return UiScale.IDENTITY
    value = app.property(ZOOM_PROPERTY)
    if not isinstance(value, int):
        return UiScale.IDENTITY
    return UiScale.of(value)


def baseline_font() -> QFont:
    """The application's original, unscaled UI font.

    The platform's own font - family, size and the operator's Windows text
    scaling - as it was before any zoom was applied. Falls back to the current
    application font when no manager exists, which is then necessarily
    unscaled.
    """
    manager = UiScaleManager.find()
    if manager is not None:
        return QFont(manager.baseline_font)
    return QFont(QApplication.font())


def relative_font(
    base: QFont,
    delta: float = 0.0,
    *,
    weight: int | None = None,
    scale: UiScale | None = None,
) -> QFont:
    """``base`` resized to the baseline size plus ``delta``, at ``scale``.

    Args:
        base: Supplies everything except the size - family, style, weight
            unless ``weight`` overrides it.
        delta: A :class:`~omr_scanner.gui.theme.FontSize` delta, in points.
        weight: A :class:`~omr_scanner.gui.theme.FontWeight`, or ``None`` to
            keep ``base``'s.
        scale: Defaults to :func:`current_scale`.

    The size is computed from :func:`baseline_font`, never from ``base``:
    ``base`` is usually a widget's current font, which is already scaled.
    """
    scale = scale if scale is not None else current_scale()
    origin = baseline_font()
    font = QFont(base)
    if origin.pointSizeF() > 0:
        font.setPointSizeF(scale.point_size(origin.pointSizeF(), delta))
    else:  # pragma: no cover - a pixel-sized platform font is unusual on Windows
        points_per_pixel = 0.75
        pixels = max(origin.pixelSize() + delta / points_per_pixel, 1.0)
        font.setPixelSize(max(1, round(pixels * scale.factor)))
    if weight is not None:
        font.setWeight(QFont.Weight(weight))
    return font


# ----------------------------------------------------------------------
# Declarations
# ----------------------------------------------------------------------
def _declaration(target: QObject) -> dict[str, Any]:
    value = target.property(DECLARATION_PROPERTY)
    return dict(value) if isinstance(value, dict) else {}


def _declare(target: QObject, updates: dict[str, Any]) -> dict[str, Any]:
    declaration = _declaration(target)
    declaration.update({key: value for key, value in updates.items() if value is not None})
    target.setProperty(DECLARATION_PROPERTY, declaration)
    return declaration


def scale_widget(
    widget: QWidget,
    *,
    fixed_width: int | None = None,
    fixed_height: int | None = None,
    minimum_width: int | None = None,
    minimum_height: int | None = None,
    maximum_width: int | None = None,
    maximum_height: int | None = None,
    icon_size: int | None = None,
    contents_margins: Sequence[int] | None = None,
) -> None:
    """Give ``widget`` interface geometry that follows the zoom.

    Every argument is the canonical 100% value - a token, or the literal the
    code used before zoom existed. Applied now at :func:`current_scale`, and
    again by the manager whenever the zoom changes. Unnamed arguments leave
    whatever the widget already has.

    For *interface* sizes only. A panel's floor that exists to keep a canvas
    usable, a document coordinate or an image size is not passed here.
    """
    declaration = _declare(
        widget,
        {
            "fixedWidth": fixed_width,
            "fixedHeight": fixed_height,
            "minimumWidth": minimum_width,
            "minimumHeight": minimum_height,
            "maximumWidth": maximum_width,
            "maximumHeight": maximum_height,
            "iconSize": icon_size,
            "contentsMargins": list(contents_margins) if contents_margins is not None else None,
        },
    )
    _apply_widget_metrics(widget, declaration, current_scale())


def set_floor(
    widget: QWidget,
    *,
    minimum_width: int | None = None,
    minimum_height: int | None = None,
) -> None:
    """Give a workspace panel a floor that zoom neither inflates nor undercuts.

    For a splitter pane or a preview whose explicit minimum keeps a region
    usable. Such a floor is *not* scaled - a 520-pixel workspace floor at 200%
    would demand more than a small display has. But an explicit minimum also
    replaces the panel's own content minimum, so at a large zoom the splitter
    would squeeze the enlarged controls inside it below their size. Away from
    100% the floor is therefore the larger of the canonical value and what the
    content needs; at 100% it is exactly the canonical value, as before zoom.
    """
    declaration = _declare(
        widget, {"floorWidth": minimum_width, "floorHeight": minimum_height}
    )
    _apply_floor(widget, declaration, current_scale())
    if widget.findChild(_FloorWatcher, FLOOR_WATCHER_NAME) is None:
        widget.installEventFilter(_FloorWatcher(widget))


FLOOR_WATCHER_NAME: Final = "omrFloorWatcher"


class _FloorWatcher(QObject):
    """Re-applies a pane's floor whenever the pane's layout changes.

    Away from 100% the floor depends on the content's own minimum, and the
    content changes - a different conflict in the decision panel, a font that
    has only just finished propagating. Recomputing on the pane's own
    ``LayoutRequest`` keeps the floor right without anyone having to remember
    to ask. Parented to the pane, so it lives and dies with it.
    """

    def __init__(self, pane: QWidget) -> None:
        super().__init__(pane)
        self.setObjectName(FLOOR_WATCHER_NAME)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest and isinstance(watched, QWidget):
            _apply_floor(watched, _declaration(watched), current_scale())
        return False


def scale_layout(
    layout: QLayout,
    *,
    margins: Sequence[int] | None = None,
    spacing: int | None = None,
    horizontal_spacing: int | None = None,
    vertical_spacing: int | None = None,
) -> None:
    """Give ``layout`` margins and spacing that follow the zoom.

    The replacement for ``setContentsMargins`` and ``setSpacing`` with a
    non-zero value. A layout that sets neither already scales: its margins
    and spacing come from the style, which the manager's proxy style scales.
    """
    declaration = _declare(
        layout,
        {
            "margins": list(margins) if margins is not None else None,
            "spacing": spacing,
            "horizontalSpacing": horizontal_spacing,
            "verticalSpacing": vertical_spacing,
        },
    )
    _apply_layout_metrics(layout, declaration, current_scale())


def add_scaled_spacing(layout: QBoxLayout, value: int) -> None:
    """``layout.addSpacing(value)``, at the current zoom and every later one."""
    layout.addSpacing(current_scale().px(value))
    register_scaled_spacer(layout, layout.count() - 1, value)


def register_scaled_spacer(layout: QBoxLayout, index: int, value: int) -> None:
    """Rescale the spacer at ``index`` of ``layout`` to ``value`` at every zoom."""
    entries = layout.property(SPACERS_PROPERTY)
    spacers = list(entries) if isinstance(entries, list) else []
    spacers.append([index, value])
    layout.setProperty(SPACERS_PROPERTY, spacers)


def set_relative_font(
    widget: QWidget, delta: float = 0.0, *, weight: int | None = None
) -> None:
    """Size ``widget``'s font as the baseline plus ``delta``, scaled.

    The replacement for ``font.setPointSizeF(font.pointSizeF() + delta)``.
    That idiom freezes the widget at whatever the application font was when
    it ran, so a zoom change would enlarge everything except the titles. This
    records the *delta*, so the title stays exactly ``delta`` points above the
    body text at 100% and the same proportion above it at every zoom.
    """
    declaration = _declare(widget, {"fontDelta": float(delta), "fontWeight": weight})
    _apply_font(widget, declaration, current_scale())


def set_scaled_stylesheet(widget: QWidget, *names: str) -> None:
    """Style ``widget`` with page-scoped sheets that follow the zoom.

    Args:
        widget: The widget to style.
        names: Keys of
            :data:`~omr_scanner.gui.theme.stylesheet.SCALED_STYLESHEETS`, in
            the order they should be concatenated.
    """
    for name in names:
        if name not in SCALED_STYLESHEETS:
            raise KeyError(f"No scaled stylesheet named {name!r}")
    declaration = _declare(widget, {"styleSheets": list(names)})
    _apply_stylesheets(widget, declaration, current_scale())


def _within_screen(widget: QWidget, width: int, height: int) -> tuple[int, int]:
    """``(width, height)`` at the current zoom, capped to ``widget``'s screen."""
    target_width, target_height = current_scale().size(width, height)
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        target_width = min(target_width, int(available.width() * 0.95))
        target_height = min(target_height, int(available.height() * 0.9))
    return target_width, target_height


def resize_scaled(widget: QWidget, width: int, height: int) -> None:
    """Give a dialog its initial size at the current zoom, within the screen.

    A dialog that asks for 760x560 at 200% would otherwise ask for more than
    a 1366x768 display has; it is capped to the available area instead, and
    its own layout and scroll areas take it from there.
    """
    widget.resize(*_within_screen(widget, width, height))


def set_dialog_minimum_size(widget: QWidget, width: int, height: int) -> None:
    """A dialog's floor at the current zoom, never larger than its screen.

    The same cap as :func:`resize_scaled`, for the same reason: a minimum
    that outgrows the display is a dialog that cannot be fully seen.
    """
    widget.setMinimumSize(*_within_screen(widget, width, height))


# ----------------------------------------------------------------------
# Applying declarations
# ----------------------------------------------------------------------
def _apply_widget_metrics(
    widget: QWidget, declaration: dict[str, Any], scale: UiScale
) -> None:
    px = scale.px
    if (value := declaration.get("fixedWidth")) is not None:
        widget.setFixedWidth(px(value))
    if (value := declaration.get("fixedHeight")) is not None:
        widget.setFixedHeight(px(value))
    if (value := declaration.get("minimumWidth")) is not None:
        widget.setMinimumWidth(px(value))
    if (value := declaration.get("minimumHeight")) is not None:
        widget.setMinimumHeight(px(value))
    if (value := declaration.get("maximumWidth")) is not None:
        widget.setMaximumWidth(px(value))
    if (value := declaration.get("maximumHeight")) is not None:
        widget.setMaximumHeight(px(value))
    if (value := declaration.get("iconSize")) is not None:
        set_icon_size = getattr(widget, "setIconSize", None)
        if callable(set_icon_size):
            set_icon_size(QSize(px(value), px(value)))
    if (value := declaration.get("contentsMargins")) is not None:
        widget.setContentsMargins(*(px(part) for part in value))


def _apply_floor(widget: QWidget, declaration: dict[str, Any], scale: UiScale) -> None:
    content = None if scale.is_identity else widget.minimumSizeHint()
    if (value := declaration.get("floorWidth")) is not None:
        width = int(value)
        if content is not None:
            width = max(width, content.width())
        if widget.minimumWidth() != width:
            widget.setMinimumWidth(width)
    if (value := declaration.get("floorHeight")) is not None:
        height = int(value)
        if content is not None:
            height = max(height, content.height())
        if widget.minimumHeight() != height:
            widget.setMinimumHeight(height)


def _apply_font(widget: QWidget, declaration: dict[str, Any], scale: UiScale) -> None:
    if "fontDelta" not in declaration:
        return
    widget.setFont(
        relative_font(
            widget.font(),
            float(declaration["fontDelta"]),
            weight=declaration.get("fontWeight"),
            scale=scale,
        )
    )


def _apply_stylesheets(
    widget: QWidget, declaration: dict[str, Any], scale: UiScale
) -> None:
    names = declaration.get("styleSheets")
    if not names:
        return
    widget.setStyleSheet("".join(SCALED_STYLESHEETS[name](scale) for name in names))


def _apply_layout_metrics(
    layout: QLayout, declaration: dict[str, Any], scale: UiScale
) -> None:
    px = scale.px
    if (margins := declaration.get("margins")) is not None:
        layout.setContentsMargins(*(px(part) for part in margins))
    if (value := declaration.get("spacing")) is not None:
        layout.setSpacing(px(value))
    if isinstance(layout, (QGridLayout, QFormLayout)):
        if (value := declaration.get("horizontalSpacing")) is not None:
            layout.setHorizontalSpacing(px(value))
        if (value := declaration.get("verticalSpacing")) is not None:
            layout.setVerticalSpacing(px(value))


def _rescale_layout_tree(layout: QLayout, scale: UiScale) -> None:
    """Re-apply ``layout``'s declaration and its spacers, and recurse."""
    declaration = _declaration(layout)
    if declaration:
        _apply_layout_metrics(layout, declaration, scale)
    spacers = layout.property(SPACERS_PROPERTY)
    if isinstance(spacers, list) and spacers and isinstance(layout, QBoxLayout):
        horizontal = layout.direction() in (
            QBoxLayout.Direction.LeftToRight,
            QBoxLayout.Direction.RightToLeft,
        )
        for index, value in spacers:
            item = layout.itemAt(int(index))
            spacer = item.spacerItem() if item is not None else None
            if spacer is None:  # the layout was rearranged; leave it be
                continue
            length = scale.px(value)
            if horizontal:
                spacer.changeSize(
                    length, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum
                )
            else:
                spacer.changeSize(
                    0, length, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
                )
        layout.invalidate()
    # Nested layouts through QObject children, never `itemAt(i).layout()`:
    # a layout reached as a `QLayoutItem*` gets a PySide wrapper that is not
    # invalidated when Qt deletes the layout. QStatusBar deletes and rebuilds
    # its nested layouts whenever it reformats, so a later pass could be handed
    # that stale wrapper for whatever reused the address - a segmentation
    # fault on Linux CI, latent on Windows. `addLayout` parents the nested
    # layout to this one, so `children()` reaches the same layouts.
    for child in layout.children():
        if isinstance(child, QLayout):
            _rescale_layout_tree(child, scale)


# ----------------------------------------------------------------------
# The proxy style
# ----------------------------------------------------------------------
class _ScaledStyle(QProxyStyle):
    """The platform style, with interface metrics multiplied by the zoom."""

    def __init__(self, base_key: str) -> None:
        super().__init__(base_key)
        self.setObjectName("omrScaledStyle")
        self._scale = UiScale.IDENTITY

    def set_scale(self, scale: UiScale) -> None:
        self._scale = scale

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        value = int(super().pixelMetric(metric, option, widget))
        if self._scale.is_identity or value <= 0 or metric not in _SCALED_METRICS:
            return value
        return self._scale.px(value)


# ----------------------------------------------------------------------
# The manager
# ----------------------------------------------------------------------
class UiScaleManager(QObject):
    """The application's interface zoom.

    One per `QApplication`, parented to it and found again with
    :meth:`find`, so a test suite that reuses one application across many
    windows also reuses one manager - and therefore one captured 100% font.

    Args:
        app: The application to manage.

    Signals:
        scale_changed: The zoom changed. Carries the new percentage.
    """

    scale_changed = Signal(int)

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.setObjectName(MANAGER_OBJECT_NAME)
        self._app = app
        self._baseline_font = QFont(app.font())
        self._scale = UiScale.IDENTITY
        self._style: _ScaledStyle | None = None
        self._owns_stylesheet = False
        self._base_style_key = app.style().name() or self._default_style_key()
        app.setProperty(ZOOM_PROPERTY, self._scale.percent)

    @staticmethod
    def _default_style_key() -> str:
        keys = QStyleFactory.keys()
        return keys[0] if keys else "Fusion"

    @classmethod
    def find(cls) -> UiScaleManager | None:
        """The running application's manager, if one has been created."""
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return None
        found = app.findChild(QObject, MANAGER_OBJECT_NAME)
        return found if isinstance(found, UiScaleManager) else None

    @classmethod
    def ensure(cls) -> UiScaleManager | None:
        """The running application's manager, created on first use.

        ``None`` only when there is no `QApplication` at all, in which case
        there is no interface to zoom either.
        """
        existing = cls.find()
        if existing is not None:
            return existing
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return None
        return cls(app)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def percent(self) -> int:
        """The current zoom, in percent."""
        return self._scale.percent

    @property
    def scale(self) -> UiScale:
        """The current zoom, as a :class:`~omr_scanner.gui.theme.UiScale`."""
        return self._scale

    @property
    def baseline_font(self) -> QFont:
        """The application font at 100%, as the platform supplied it."""
        return QFont(self._baseline_font)

    @property
    def owns_stylesheet(self) -> bool:
        """Whether this manager composes the application stylesheet."""
        return self._owns_stylesheet

    # ------------------------------------------------------------------
    # Changing it
    # ------------------------------------------------------------------
    def install(self) -> None:
        """Take over the application stylesheet and the style, at the current zoom.

        Called once by
        :func:`~omr_scanner.gui.application.configure_application`. A test
        that builds a window without configuring the application gets a
        manager that scales fonts and widgets but leaves the stylesheet alone,
        so tests that never asked for the stylesheet do not acquire one.
        """
        self._owns_stylesheet = True
        self._install_style()
        self._apply()

    def set_percent(self, percent: int) -> bool:
        """Zoom the interface to ``percent``, clamped to the supported range.

        Returns:
            Whether the zoom changed. ``False`` when it was already there,
            in which case nothing is re-applied.
        """
        scale = UiScale.of(percent)
        if scale == self._scale:
            return False
        self._scale = scale
        self._apply()
        self.scale_changed.emit(scale.percent)
        return True

    def _install_style(self) -> None:
        if self._style is not None:
            return
        style = _ScaledStyle(self._base_style_key)
        style.set_scale(self._scale)
        self._app.setStyle(style)
        self._style = style

    def _scaled_font(self) -> QFont:
        font = QFont(self._baseline_font)
        if self._baseline_font.pointSizeF() > 0:
            font.setPointSizeF(self._baseline_font.pointSizeF() * self._scale.factor)
        else:  # pragma: no cover - see relative_font
            font.setPixelSize(max(1, round(self._baseline_font.pixelSize() * self._scale.factor)))
        return font

    def _apply(self) -> None:
        """Push the current zoom into the application and every widget.

        Order matters. The proxy style and the font first; then the
        application sheet, whose re-application is also what makes a widget
        styled by the global sheet pick up the new font; then each widget's
        declared metrics, fonts and own sheet; and the custom widgets' hooks
        last, so they measure text in the font it will actually be drawn in.
        """
        scale = self._scale
        app = self._app
        app.setProperty(ZOOM_PROPERTY, scale.percent)
        if self._style is None and not scale.is_identity:
            self._install_style()
        if self._style is not None:
            self._style.set_scale(scale)

        target = self._scaled_font()
        if app.font() != target:
            app.setFont(target)

        if self._owns_stylesheet:
            app.setStyleSheet(application_stylesheet(scale))
        elif app.styleSheet():
            app.setStyleSheet(app.styleSheet())

        widgets = [
            widget
            for widget in app.allWidgets()
            if isinstance(widget, QWidget) and shiboken6.isValid(widget)
        ]
        for widget in widgets:
            self._rescale_declared(widget, scale)
        for widget in widgets:
            if not shiboken6.isValid(widget):
                continue
            if isinstance(widget, ScaleAware):
                try:
                    widget.apply_ui_scale(scale)
                except Exception:  # pragma: no cover - one widget must not stop the zoom
                    logger.exception("Could not rescale %s", widget.objectName())
        # Floors last: each is measured against its content's new minimum,
        # so the layouts that the font and metric changes invalidated are
        # brought up to date first.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        for widget in widgets:
            if shiboken6.isValid(widget):
                declaration = _declaration(widget)
                if "floorWidth" in declaration or "floorHeight" in declaration:
                    _apply_floor(widget, declaration, scale)
        # Debug, not info: the open project's log records INFO, and a display
        # preference has no business appearing in an examination's record.
        logger.debug("Interface zoom applied: %d%%", scale.percent)

    @staticmethod
    def _rescale_declared(widget: QWidget, scale: UiScale) -> None:
        try:
            declaration = _declaration(widget)
            if declaration:
                _apply_widget_metrics(widget, declaration, scale)
                _apply_font(widget, declaration, scale)
            if declaration.get("styleSheets"):
                _apply_stylesheets(widget, declaration, scale)
            elif widget.styleSheet():
                # Qt only pushes a new application font into a widget styled
                # by its own sheet when that sheet is set again.
                widget.setStyleSheet(widget.styleSheet())
            layout = widget.layout()
            if layout is not None:
                _rescale_layout_tree(layout, scale)
        except Exception:  # pragma: no cover - one widget must not stop the zoom
            logger.exception("Could not rescale %s", widget.objectName())


__all__ = [
    "DECLARATION_PROPERTY",
    "ZOOM_PROPERTY",
    "ScaleAware",
    "UiScaleManager",
    "add_scaled_spacing",
    "baseline_font",
    "current_scale",
    "register_scaled_spacer",
    "relative_font",
    "resize_scaled",
    "scale_layout",
    "scale_widget",
    "set_dialog_minimum_size",
    "set_floor",
    "set_relative_font",
    "set_scaled_stylesheet",
]
