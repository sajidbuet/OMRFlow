"""Qt stylesheets composed from the design tokens.

Purpose:
    Turn :mod:`omr_scanner.gui.theme.tokens` into the two stylesheet strings
    the application applies: one global sheet for the whole application, and
    the pre-existing page-scoped sheet for the editor toolbars.

Responsibilities:
    * :func:`application_stylesheet` - applied once, to the `QApplication`, so
      that every window and dialog (including `QMessageBox`, which the
      application does not construct itself) inherits it.
    * :data:`TEMPLATE_DESIGNER_STYLESHEET` - unchanged in scope and purpose,
      now expressed in the shared tokens instead of its own private palette.

What does NOT belong here:
    * Any widget construction, and any colour literal. Every value comes from
      :mod:`.tokens`.
    * Per-widget geometry. A stylesheet that sets fixed heights fights Qt's
      layout system and breaks under Windows text scaling; sizes here are
      confined to paddings, radii and border widths, all of which Qt scales.

Why one global sheet rather than per-widget styling:
    `QMessageBox`, `QFileDialog` and `QInputDialog` are created by Qt or by
    convenience static methods, so there is no constructor to style. Setting
    the sheet on the application is the only way their buttons and labels
    match the rest of the interface.

Why button variants are a dynamic property:
    ``QPushButton[variant="primary"]`` lets the one primary action on a page
    opt in with ``setProperty("variant", "primary")`` while every other button
    keeps the neutral default, without a subclass per variant. A widget whose
    property changes after it is shown needs its style repolished - see
    :func:`omr_scanner.gui.widgets.buttons.set_button_variant`, which is the
    only place that sets it.
"""

from __future__ import annotations

from typing import Final

from omr_scanner.gui.theme.tokens import (
    Color,
    Radius,
    Spacing,
    Stroke,
)

VARIANT_PROPERTY: Final = "variant"
"""Dynamic property naming a button's role: ``"primary"``, ``"destructive"``,
or absent for the neutral default."""

VARIANT_PRIMARY: Final = "primary"
VARIANT_DESTRUCTIVE: Final = "destructive"

_CONTROL_H_PADDING: Final = 12
_CONTROL_V_PADDING: Final = 6
_CONTROL_MIN_HEIGHT: Final = 26
"""A minimum, never a fixed height: the control still grows when the platform
font or Windows text scaling is larger, which a fixed height would clip."""

_SPIN_BUTTON_WIDTH: Final = 18
"""Width reserved for a spin box's up/down buttons.

Wide enough to be an easy mouse target - Fitts's law applies to a 9-pixel-tall
half-button more than to anything else in the interface - and the value the
line edit's `padding-right` reserves, so the two cannot disagree and let the
editor cover the arrows again."""


def application_stylesheet() -> str:
    """Return the global stylesheet, applied to the `QApplication`.

    Returns:
        A Qt stylesheet covering the application shell, the common controls
        and the container widgets. Composed on each call rather than stored as
        a module constant so that a test can assert it reflects the tokens
        rather than a stale copy of them.
    """
    return f"""
/* ---------------------------------------------------------------- Base */
QWidget {{
    color: {Color.TEXT_PRIMARY};
}}

QMainWindow, QDialog {{
    background: {Color.SURFACE_SUNKEN};
}}

QMainWindow::separator {{
    background: {Color.BORDER};
    width: {Stroke.HAIRLINE}px;
    height: {Stroke.HAIRLINE}px;
}}

QLabel {{
    background: transparent;
}}

QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled, QGroupBox:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QToolTip {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
    border-radius: {Radius.SM}px;
    padding: {Spacing.XS}px {Spacing.SM}px;
}}

/* -------------------------------------------------- Application shell */
/* The window's own edge. A frameless window gets no drop shadow from
   Windows, so this hairline is what separates the application from whatever
   is behind it - without it the shell bleeds into a light desktop. */
#appCentralWidget {{
    background: {Color.SURFACE_SUNKEN};
    border: {Stroke.HAIRLINE}px solid {Color.BORDER_STRONG};
}}

#appChrome {{
    background: {Color.SURFACE};
    border-bottom: {Stroke.HAIRLINE}px solid {Color.BORDER};
}}

#appMenuButton {{
    background: transparent;
    border: {Stroke.BORDER}px solid transparent;
    border-radius: {Radius.MD}px;
}}

#appMenuButton:hover {{
    background: {Color.SURFACE_HOVER};
}}

#appMenuButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

#appMenuButton:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

#appMenuButton::menu-indicator {{
    image: none;
    width: 0px;
}}

QToolButton[chromeControl="true"] {{
    background: transparent;
    border: {Stroke.BORDER}px solid transparent;
    border-radius: {Radius.SM}px;
    padding: 0px;
}}

QToolButton[chromeControl="true"]:hover {{
    background: {Color.SURFACE_HOVER};
}}

QToolButton[chromeControl="true"]:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QToolButton[chromeControl="true"]:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

QToolButton[chromeControl="true"]:disabled {{
    color: {Color.TEXT_DISABLED};
}}

#appChromeSeparator {{
    color: {Color.BORDER_STRONG};
}}

#workflowRibbon, #workflowRibbonScroll, #workflowRibbonStrip {{
    background: transparent;
    border: none;
}}

#appFooter {{
    background: {Color.SURFACE_MUTED};
    border-top: {Stroke.HAIRLINE}px solid {Color.BORDER};
}}

#appFooterVersion {{
    color: {Color.TEXT_PRIMARY};
}}

#appFooterProject, #appFooterCredit, #appFooterLicence, #appFooterSeparator {{
    color: {Color.TEXT_SECONDARY};
}}

#appFooterStatus {{
    color: {Color.TEXT_PRIMARY};
}}

QStatusBar {{
    background: {Color.SURFACE_MUTED};
    border-top: {Stroke.HAIRLINE}px solid {Color.BORDER};
    color: {Color.TEXT_SECONDARY};
}}

QStatusBar::item {{
    border: none;
}}

/* ------------------------------------------------------------- Pages */
#pageTitle {{
    color: {Color.TEXT_PRIMARY};
}}

#pageSummary, #pageNote {{
    color: {Color.TEXT_SECONDARY};
}}

#pageHeaderRule, #cardDivider {{
    background: {Color.BORDER};
    border: none;
}}

/* ------------------------------------------------------------- Cards */
QFrame[card="true"] {{
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-radius: {Radius.LG}px;
}}

QFrame[card="sunken"] {{
    background: {Color.SURFACE_SUNKEN};
    border: {Stroke.BORDER}px dashed {Color.BORDER_DASHED};
    border-radius: {Radius.LG}px;
}}

#cardTitle {{
    color: {Color.TEXT_PRIMARY};
}}

#emptyStateTitle {{
    color: {Color.TEXT_PRIMARY};
}}

#emptyStateBody, #cardEmptyBody {{
    color: {Color.TEXT_TERTIARY};
}}

/* ----------------------------------------------------------- Buttons */
QPushButton {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
    border-radius: {Radius.MD}px;
    padding: {_CONTROL_V_PADDING}px {_CONTROL_H_PADDING}px;
    min-height: {_CONTROL_MIN_HEIGHT}px;
}}

QPushButton:hover {{
    background: {Color.SURFACE_HOVER};
}}

QPushButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QPushButton:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

QPushButton:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border-color: {Color.BORDER};
}}

QPushButton:default {{
    border-color: {Color.BORDER_STRONG};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"] {{
    color: {Color.TEXT_ON_PRIMARY};
    background: {Color.PRIMARY};
    border: {Stroke.BORDER}px solid {Color.PRIMARY};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]:hover {{
    background: {Color.PRIMARY_HOVER};
    border-color: {Color.PRIMARY_HOVER};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]:pressed {{
    background: {Color.PRIMARY_PRESSED};
    border-color: {Color.PRIMARY_PRESSED};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.TEXT_ON_PRIMARY};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]:disabled {{
    color: {Color.TEXT_ON_PRIMARY_DISABLED};
    background: {Color.PRIMARY_DISABLED};
    border-color: {Color.PRIMARY_DISABLED};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_DESTRUCTIVE}"] {{
    color: {Color.DESTRUCTIVE};
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.DESTRUCTIVE};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_DESTRUCTIVE}"]:hover {{
    color: {Color.TEXT_ON_PRIMARY};
    background: {Color.DESTRUCTIVE};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_DESTRUCTIVE}"]:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border-color: {Color.BORDER};
}}

QToolButton {{
    color: {Color.TEXT_PRIMARY};
    background: transparent;
    border: {Stroke.BORDER}px solid transparent;
    border-radius: {Radius.MD}px;
    padding: {Spacing.XS}px {Spacing.SM}px;
}}

QToolButton:hover {{
    background: {Color.SURFACE_HOVER};
}}

QToolButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QToolButton:checked {{
    background: {Color.PRIMARY_SOFT};
    border-color: {Color.PRIMARY};
}}

QToolButton:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

QToolButton:disabled {{
    color: {Color.TEXT_DISABLED};
}}

/* ------------------------------------------------------------- Inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
    border-radius: {Radius.MD}px;
    padding: {_CONTROL_V_PADDING}px {Spacing.SM}px;
    min-height: {_CONTROL_MIN_HEIGHT}px;
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border-color: {Color.BORDER};
}}

QLineEdit[invalid="true"], QSpinBox[invalid="true"],
QDoubleSpinBox[invalid="true"], QComboBox[invalid="true"] {{
    border: {Stroke.FOCUS_RING}px solid {Color.DESTRUCTIVE};
}}

/* Spin boxes: the buttons need explicit geometry, or the editor covers them.

   Styling a `QSpinBox` at all switches it to `QStyleSheetStyle`, which lays
   the line edit across the whole padded content rect unless the up/down
   buttons declare a width. With only the `padding` rule above, the editor
   spanned x=10..126 of a 160px control while the buttons sat at 112..136 -
   so `childAt()` over the up arrow returned the `QLineEdit`. The arrows were
   drawn, but the edit widget was on top of them: hovering showed an I-beam
   and clicking put the caret in the text instead of stepping the value.

   Reserving the width with `padding-right` and pinning both buttons to the
   border box fixes the hit-testing rather than the appearance. Qt's own
   auto-repeat, keyboard stepping and text entry are untouched. */
QAbstractSpinBox {{
    padding-right: {_SPIN_BUTTON_WIDTH + Spacing.XS}px;
}}

QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    width: {_SPIN_BUTTON_WIDTH}px;
    border: none;
    border-radius: 0px;
    background: transparent;
}}

QAbstractSpinBox::up-button {{
    subcontrol-position: top right;
    margin: {Stroke.BORDER}px {Stroke.BORDER}px 0px 0px;
}}

QAbstractSpinBox::down-button {{
    subcontrol-position: bottom right;
    margin: 0px {Stroke.BORDER}px {Stroke.BORDER}px 0px;
}}

QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {Color.SURFACE_HOVER};
}}

QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

/* `width`/`height` rather than an image: Qt draws its own arrow primitive at
   this size, so the control needs no bundled asset and follows the palette. */
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {{
    width: {Spacing.SM}px;
    height: {Spacing.SM}px;
}}

QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off,
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    opacity: 0.4;
}}

QComboBox::drop-down {{
    border: none;
    width: {Spacing.XL}px;
}}

QComboBox QAbstractItemView {{
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
    outline: none;
}}

QCheckBox, QRadioButton {{
    background: transparent;
    spacing: {Spacing.SM}px;
}}

QCheckBox:focus, QRadioButton:focus {{
    color: {Color.TEXT_PRIMARY};
}}

/* ------------------------------------------------------- Group boxes */
QGroupBox {{
    background: transparent;
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-radius: {Radius.LG}px;
    margin-top: {Spacing.MD}px;
    padding: {Spacing.MD}px {Spacing.MD}px {Spacing.SM}px {Spacing.MD}px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {Spacing.MD}px;
    padding: 0px {Spacing.XS}px;
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE_SUNKEN};
}}

/* ------------------------------------------------------------ Tables */
QHeaderView::section {{
    color: {Color.TEXT_SECONDARY};
    background: {Color.SURFACE_MUTED};
    border: none;
    border-right: {Stroke.HAIRLINE}px solid {Color.BORDER};
    border-bottom: {Stroke.HAIRLINE}px solid {Color.BORDER};
    padding: {Spacing.XS}px {Spacing.SM}px;
    font-weight: 600;
}}

QHeaderView::section:last {{
    border-right: none;
}}

QTableView, QTableWidget, QTreeView, QListView, QListWidget {{
    background: {Color.SURFACE};
    alternate-background-color: {Color.SURFACE_SUNKEN};
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-radius: {Radius.MD}px;
    gridline-color: {Color.BORDER};
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
    outline: none;
}}

QTableView::item, QTreeView::item, QListView::item, QListWidget::item {{
    padding: {Spacing.XS}px {Spacing.SM}px;
}}

QTableView::item:hover, QTreeView::item:hover, QListWidget::item:hover {{
    background: {Color.SURFACE_HOVER};
}}

QTableView::item:selected, QTreeView::item:selected, QListWidget::item:selected {{
    background: {Color.PRIMARY};
    color: {Color.TEXT_ON_PRIMARY};
}}

QTableCornerButton::section {{
    background: {Color.SURFACE_MUTED};
    border: none;
}}

/* -------------------------------------------------------------- Tabs */
QTabWidget::pane {{
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-radius: {Radius.MD}px;
    top: -{Stroke.HAIRLINE}px;
}}

QTabBar::tab {{
    color: {Color.TEXT_SECONDARY};
    background: {Color.SURFACE_SUNKEN};
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-bottom: none;
    border-top-left-radius: {Radius.MD}px;
    border-top-right-radius: {Radius.MD}px;
    padding: {_CONTROL_V_PADDING}px {Spacing.MD}px;
    margin-right: {Spacing.XXS}px;
}}

QTabBar::tab:selected {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border-bottom: {Stroke.ACCENT_RULE}px solid {Color.PRIMARY};
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background: {Color.SURFACE_HOVER};
}}

QTabBar::tab:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QTabBar::tab:focus {{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}

/* ---------------------------------------------------- Progress bars */
QProgressBar {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE_MUTED};
    border: {Stroke.BORDER}px solid {Color.BORDER};
    border-radius: {Radius.SM}px;
    text-align: center;
    min-height: {Spacing.LG}px;
}}

QProgressBar::chunk {{
    background: {Color.PRIMARY};
    border-radius: {Radius.SM - 1}px;
}}

/* ------------------------------------------------------- Scroll bars */
QScrollBar:vertical {{
    background: transparent;
    width: {Spacing.MD}px;
    margin: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: {Spacing.MD}px;
    margin: 0px;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {Color.BORDER_STRONG};
    border-radius: {Radius.SM}px;
    min-height: {Spacing.XL}px;
    min-width: {Spacing.XL}px;
}}

QScrollBar::handle:hover {{
    background: {Color.TEXT_TERTIARY};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ------------------------------------------------------------- Menus */
QMenuBar {{
    background: {Color.SURFACE};
    border-bottom: {Stroke.HAIRLINE}px solid {Color.BORDER};
}}

QMenuBar::item {{
    padding: {Spacing.XS}px {Spacing.MD}px;
    background: transparent;
}}

QMenuBar::item:selected {{
    background: {Color.SURFACE_HOVER};
    border-radius: {Radius.SM}px;
}}

QMenu {{
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
    border-radius: {Radius.MD}px;
    padding: {Spacing.XS}px;
}}

QMenu::item {{
    padding: {Spacing.SM}px {Spacing.XL}px {Spacing.SM}px {Spacing.MD}px;
    border-radius: {Radius.SM}px;
}}

QMenu::item:selected {{
    background: {Color.PRIMARY};
    color: {Color.TEXT_ON_PRIMARY};
}}

QMenu::item:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QMenu::separator {{
    height: {Stroke.HAIRLINE}px;
    background: {Color.BORDER};
    margin: {Spacing.XS}px {Spacing.SM}px;
}}

QMenu::right-arrow {{
    width: {Spacing.MD}px;
}}

/* -------------------------------------------------------- Separators */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {Color.BORDER};
}}

QSplitter::handle {{
    background: {Color.BORDER};
}}

QSplitter::handle:horizontal {{
    width: {Stroke.HAIRLINE}px;
}}

QSplitter::handle:vertical {{
    height: {Stroke.HAIRLINE}px;
}}
"""


TEMPLATE_DESIGNER_STYLESHEET: Final = f"""
QToolBar {{
    background: {Color.SURFACE_MUTED};
    border: none;
    spacing: {Spacing.XXS}px;
    padding: {Spacing.XXS}px;
}}

QToolButton {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {Stroke.BORDER}px solid transparent;
    border-radius: {Radius.SM}px;
    padding: {Spacing.XS}px {Spacing.SM}px;
}}

QToolButton:hover {{
    background: {Color.SURFACE_HOVER};
    border: {Stroke.BORDER}px solid {Color.BORDER_STRONG};
}}

QToolButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QToolButton:checked {{
    background: {Color.PRIMARY_SOFT};
    border: {Stroke.BORDER}px solid {Color.PRIMARY};
}}

QToolButton:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border: {Stroke.BORDER}px solid transparent;
}}

QToolBar::separator {{
    background: {Color.BORDER_STRONG};
    width: {Stroke.HAIRLINE}px;
    margin: {Spacing.XS}px {Spacing.XS}px;
}}

QPushButton {{
    color: {Color.TEXT_PRIMARY};
}}

QPushButton:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QLabel:disabled {{
    color: {Color.TEXT_DISABLED};
}}
"""
"""Applied to the editor pages only (template designer, calibration, scan,
resolve), each by its own ``setStyleSheet(...)`` call, so no other page's
controls change appearance.

Unchanged in scope and behaviour from the version that predated the design
system - every state it gave the toolbar explicit colours for still has one.
What changed is where the colours come from: the toolbar's checked state now
uses the accent tint the rest of the application uses for a selected control,
instead of a light blue that existed only here. The state is still never
communicated by colour alone - each toggle's tooltip names its state, and the
Grid toggle's own overlay is visible on the canvas."""


__all__ = [
    "TEMPLATE_DESIGNER_STYLESHEET",
    "VARIANT_DESTRUCTIVE",
    "VARIANT_PRIMARY",
    "VARIANT_PROPERTY",
    "application_stylesheet",
]
