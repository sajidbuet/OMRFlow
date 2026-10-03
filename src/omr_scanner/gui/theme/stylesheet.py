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
      confined to paddings, minimum heights, radii and border widths.

Why every sheet takes a :class:`~omr_scanner.gui.theme.scale.UiScale`:
    The operator's interface zoom has to reach the paddings and bar widths
    as well as the text, or a 150% interface is 150% text crammed into 100%
    controls. Each sheet is therefore a function of the scale, composed from
    the canonical tokens on every call; the ``*_STYLESHEET`` constants are the
    same functions evaluated at 100%, kept for the callers and tests that
    only ever wanted the canonical design.

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

from collections.abc import Callable
from typing import Final

from omr_scanner.gui.theme.scale import UiScale
from omr_scanner.gui.theme.tokens import (
    Color,
    FontWeight,
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


def application_stylesheet(scale: UiScale = UiScale.IDENTITY) -> str:
    """Return the global stylesheet, applied to the `QApplication`.

    Args:
        scale: The interface zoom. Every padding, minimum height, radius,
            border and bar thickness below is passed through it; colours are
            not. The default is the canonical design, and at 100% the output
            is exactly what this function returned before zoom existed.

    Returns:
        A Qt stylesheet covering the application shell, the common controls
        and the container widgets. Composed on each call rather than stored as
        a module constant so that a test can assert it reflects the tokens
        rather than a stale copy of them - and so that it can be recomposed
        for a new zoom without anything remembering the previous one.
    """
    px = scale.px
    st = scale.stroke
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
    width: {st(Stroke.HAIRLINE)}px;
    height: {st(Stroke.HAIRLINE)}px;
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
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
    border-radius: {px(Radius.SM)}px;
    padding: {px(Spacing.XS)}px {px(Spacing.SM)}px;
}}

/* -------------------------------------------------- Application shell */
/* The window's own edge. A frameless window gets no drop shadow from
   Windows, so this hairline is what separates the application from whatever
   is behind it - without it the shell bleeds into a light desktop. */
#appCentralWidget {{
    background: {Color.SURFACE_SUNKEN};
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER_STRONG};
}}

#appChrome {{
    background: {Color.SURFACE};
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
}}

#appMenuButton {{
    background: transparent;
    border: {st(Stroke.BORDER)}px solid transparent;
    border-radius: {px(Radius.MD)}px;
}}

#appMenuButton:hover {{
    background: {Color.SURFACE_HOVER};
}}

#appMenuButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

#appMenuButton:focus {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
}}

#appMenuButton::menu-indicator {{
    image: none;
    width: 0px;
}}

QToolButton[chromeControl="true"] {{
    background: transparent;
    border: {st(Stroke.BORDER)}px solid transparent;
    border-radius: {px(Radius.SM)}px;
    padding: 0px;
}}

QToolButton[chromeControl="true"]:hover {{
    background: {Color.SURFACE_HOVER};
}}

QToolButton[chromeControl="true"]:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QToolButton[chromeControl="true"]:focus {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
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
    border-top: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
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
    border-top: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
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
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-radius: {px(Radius.LG)}px;
}}

QFrame[card="sunken"] {{
    background: {Color.SURFACE_SUNKEN};
    border: {st(Stroke.BORDER)}px dashed {Color.BORDER_DASHED};
    border-radius: {px(Radius.LG)}px;
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
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
    border-radius: {px(Radius.MD)}px;
    padding: {px(_CONTROL_V_PADDING)}px {px(_CONTROL_H_PADDING)}px;
    min-height: {px(_CONTROL_MIN_HEIGHT)}px;
}}

QPushButton:hover {{
    background: {Color.SURFACE_HOVER};
}}

QPushButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QPushButton:focus {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
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
    border: {st(Stroke.BORDER)}px solid {Color.PRIMARY};
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
    border: {st(Stroke.FOCUS_RING)}px solid {Color.TEXT_ON_PRIMARY};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_PRIMARY}"]:disabled {{
    color: {Color.TEXT_ON_PRIMARY_DISABLED};
    background: {Color.PRIMARY_DISABLED};
    border-color: {Color.PRIMARY_DISABLED};
}}

QPushButton[{VARIANT_PROPERTY}="{VARIANT_DESTRUCTIVE}"] {{
    color: {Color.DESTRUCTIVE};
    background: {Color.SURFACE};
    border: {st(Stroke.BORDER)}px solid {Color.DESTRUCTIVE};
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
    border: {st(Stroke.BORDER)}px solid transparent;
    border-radius: {px(Radius.MD)}px;
    padding: {px(Spacing.XS)}px {px(Spacing.SM)}px;
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
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
}}

QToolButton:disabled {{
    color: {Color.TEXT_DISABLED};
}}

/* ------------------------------------------------------------- Inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
    border-radius: {px(Radius.MD)}px;
    padding: {px(_CONTROL_V_PADDING)}px {px(Spacing.SM)}px;
    min-height: {px(_CONTROL_MIN_HEIGHT)}px;
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
}}

QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border-color: {Color.BORDER};
}}

QLineEdit[invalid="true"], QSpinBox[invalid="true"],
QDoubleSpinBox[invalid="true"], QComboBox[invalid="true"] {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.DESTRUCTIVE};
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
    padding-right: {px(_SPIN_BUTTON_WIDTH + Spacing.XS)}px;
}}

QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    width: {px(_SPIN_BUTTON_WIDTH)}px;
    border: none;
    border-radius: 0px;
    background: transparent;
}}

QAbstractSpinBox::up-button {{
    subcontrol-position: top right;
    margin: {st(Stroke.BORDER)}px {st(Stroke.BORDER)}px 0px 0px;
}}

QAbstractSpinBox::down-button {{
    subcontrol-position: bottom right;
    margin: 0px {st(Stroke.BORDER)}px {st(Stroke.BORDER)}px 0px;
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
    width: {px(Spacing.SM)}px;
    height: {px(Spacing.SM)}px;
}}

QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off,
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
    opacity: 0.4;
}}

QComboBox::drop-down {{
    border: none;
    width: {px(Spacing.XL)}px;
}}

QComboBox QAbstractItemView {{
    background: {Color.SURFACE};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
    outline: none;
}}

QCheckBox, QRadioButton {{
    background: transparent;
    spacing: {px(Spacing.SM)}px;
}}

QCheckBox:focus, QRadioButton:focus {{
    color: {Color.TEXT_PRIMARY};
}}

/* ------------------------------------------------------- Group boxes */
QGroupBox {{
    background: transparent;
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-radius: {px(Radius.LG)}px;
    margin-top: {px(Spacing.MD)}px;
    padding: {px(Spacing.MD)}px {px(Spacing.MD)}px {px(Spacing.SM)}px {px(Spacing.MD)}px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {px(Spacing.MD)}px;
    padding: 0px {px(Spacing.XS)}px;
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE_SUNKEN};
}}

/* ------------------------------------------------------------ Tables */
QHeaderView::section {{
    color: {Color.TEXT_SECONDARY};
    background: {Color.SURFACE_MUTED};
    border: none;
    border-right: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    padding: {px(Spacing.XS)}px {px(Spacing.SM)}px;
    font-weight: 600;
}}

QHeaderView::section:last {{
    border-right: none;
}}

QTableView, QTableWidget, QTreeView, QListView, QListWidget {{
    background: {Color.SURFACE};
    alternate-background-color: {Color.SURFACE_SUNKEN};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-radius: {px(Radius.MD)}px;
    gridline-color: {Color.BORDER};
    selection-background-color: {Color.PRIMARY};
    selection-color: {Color.TEXT_ON_PRIMARY};
    outline: none;
}}

QTableView::item, QTreeView::item, QListView::item, QListWidget::item {{
    padding: {px(Spacing.XS)}px {px(Spacing.SM)}px;
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
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-radius: {px(Radius.MD)}px;
    top: -{st(Stroke.HAIRLINE)}px;
}}

QTabBar::tab {{
    color: {Color.TEXT_SECONDARY};
    background: {Color.SURFACE_SUNKEN};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-bottom: none;
    border-top-left-radius: {px(Radius.MD)}px;
    border-top-right-radius: {px(Radius.MD)}px;
    padding: {px(_CONTROL_V_PADDING)}px {px(Spacing.MD)}px;
    margin-right: {px(Spacing.XXS)}px;
}}

QTabBar::tab:selected {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border-bottom: {st(Stroke.ACCENT_RULE)}px solid {Color.PRIMARY};
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background: {Color.SURFACE_HOVER};
}}

QTabBar::tab:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QTabBar::tab:focus {{
    border: {st(Stroke.FOCUS_RING)}px solid {Color.FOCUS};
}}

/* ---------------------------------------------------- Progress bars */
QProgressBar {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE_MUTED};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER};
    border-radius: {px(Radius.SM)}px;
    text-align: center;
    min-height: {px(Spacing.LG)}px;
}}

QProgressBar::chunk {{
    background: {Color.PRIMARY};
    border-radius: {max(px(Radius.SM) - 1, 0)}px;
}}

/* ------------------------------------------------------- Scroll bars */
QScrollBar:vertical {{
    background: transparent;
    width: {px(Spacing.MD)}px;
    margin: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: {px(Spacing.MD)}px;
    margin: 0px;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {Color.BORDER_STRONG};
    border-radius: {px(Radius.SM)}px;
    min-height: {px(Spacing.XL)}px;
    min-width: {px(Spacing.XL)}px;
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
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
}}

QMenuBar::item {{
    padding: {px(Spacing.XS)}px {px(Spacing.MD)}px;
    background: transparent;
}}

QMenuBar::item:selected {{
    background: {Color.SURFACE_HOVER};
    border-radius: {px(Radius.SM)}px;
}}

QMenu {{
    background: {Color.SURFACE};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
    border-radius: {px(Radius.MD)}px;
    padding: {px(Spacing.XS)}px;
}}

QMenu::item {{
    padding: {px(Spacing.SM)}px {px(Spacing.XL)}px {px(Spacing.SM)}px {px(Spacing.MD)}px;
    border-radius: {px(Radius.SM)}px;
}}

QMenu::item:selected {{
    background: {Color.PRIMARY};
    color: {Color.TEXT_ON_PRIMARY};
}}

QMenu::item:disabled {{
    color: {Color.TEXT_DISABLED};
}}

QMenu::separator {{
    height: {st(Stroke.HAIRLINE)}px;
    background: {Color.BORDER};
    margin: {px(Spacing.XS)}px {px(Spacing.SM)}px;
}}

QMenu::right-arrow {{
    width: {px(Spacing.MD)}px;
}}

/* -------------------------------------------------------- Separators */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {Color.BORDER};
}}

QSplitter::handle {{
    background: {Color.BORDER};
}}

QSplitter::handle:horizontal {{
    width: {st(Stroke.HAIRLINE)}px;
}}

QSplitter::handle:vertical {{
    height: {st(Stroke.HAIRLINE)}px;
}}
"""


def template_designer_stylesheet(scale: UiScale = UiScale.IDENTITY) -> str:
    """The editor pages' toolbar sheet at ``scale`` - see the constant below."""
    px = scale.px
    st = scale.stroke
    return f"""
QToolBar {{
    background: {Color.SURFACE_MUTED};
    border: none;
    spacing: {px(Spacing.XXS)}px;
    padding: {px(Spacing.XXS)}px;
}}

QToolButton {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.SURFACE};
    border: {st(Stroke.BORDER)}px solid transparent;
    border-radius: {px(Radius.SM)}px;
    padding: {px(Spacing.XS)}px {px(Spacing.SM)}px;
}}

QToolButton:hover {{
    background: {Color.SURFACE_HOVER};
    border: {st(Stroke.BORDER)}px solid {Color.BORDER_STRONG};
}}

QToolButton:pressed {{
    background: {Color.SURFACE_PRESSED};
}}

QToolButton:checked {{
    background: {Color.PRIMARY_SOFT};
    border: {st(Stroke.BORDER)}px solid {Color.PRIMARY};
}}

QToolButton:disabled {{
    color: {Color.TEXT_DISABLED};
    background: {Color.SURFACE_DISABLED};
    border: {st(Stroke.BORDER)}px solid transparent;
}}

QToolBar::separator {{
    background: {Color.BORDER_STRONG};
    width: {st(Stroke.HAIRLINE)}px;
    margin: {px(Spacing.XS)}px {px(Spacing.XS)}px;
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


TEMPLATE_DESIGNER_STYLESHEET: Final = template_designer_stylesheet()
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


CANDIDATE_STATE_PROPERTY: Final = "candidateState"
"""Dynamic property naming what one value button on the Resolve stage means.

A property plus a stylesheet rule rather than per-button ``setStyleSheet``
calls: there are eleven of these buttons on a roll-number position, they are
rebuilt on every selection, and colouring them one at a time is how a palette
ends up written out in a page module."""

CANDIDATE_MACHINE: Final = "machine"
"""This symbol is one the engine read on the paper."""

CANDIDATE_CHOSEN: Final = "chosen"
"""The reviewer has picked this symbol but has not committed it."""

def resolve_stage_stylesheet(scale: UiScale = UiScale.IDENTITY) -> str:
    """The Resolve stage's own rules at ``scale`` - see the constant below."""
    px = scale.px
    st = scale.stroke
    return f"""
QPushButton[{CANDIDATE_STATE_PROPERTY}="{CANDIDATE_MACHINE}"] {{
    background: {Color.ATTENTION_SOFT};
    border: {st(Stroke.BORDER)}px solid {Color.ATTENTION};
    font-weight: {FontWeight.SEMIBOLD};
}}

QPushButton[{CANDIDATE_STATE_PROPERTY}="{CANDIDATE_MACHINE}"]:hover {{
    background: {Color.ATTENTION_HOVER};
}}

QPushButton[{CANDIDATE_STATE_PROPERTY}="{CANDIDATE_CHOSEN}"] {{
    color: {Color.TEXT_ON_PRIMARY};
    background: {Color.PRIMARY};
    border: {st(Stroke.FOCUS_RING)}px solid {Color.PRIMARY_PRESSED};
    font-weight: {FontWeight.BOLD};
}}

QPushButton[{CANDIDATE_STATE_PROPERTY}="{CANDIDATE_CHOSEN}"]:hover {{
    background: {Color.PRIMARY_HOVER};
}}

QLabel#resolveSectionHeading {{
    color: {Color.TEXT_TERTIARY};
    font-weight: {FontWeight.SEMIBOLD};
}}

QLabel#resolveOperatorBadge {{
    color: {Color.TEXT_SECONDARY};
    background: {Color.SURFACE_MUTED};
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-radius: {px(Radius.SM)}px;
    padding: {px(1)}px {px(Spacing.SM)}px;
}}

QFrame#conflictProvenanceStrip {{
    background: {Color.SURFACE_SUNKEN};
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-radius: {px(Radius.SM)}px;
}}

QTableWidget#conflictQueueTable::item:selected {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.PRIMARY_SOFT};
    border-top: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
}}
"""


RESOLVE_STAGE_STYLESHEET: Final = resolve_stage_stylesheet()
"""Appended to :data:`TEMPLATE_DESIGNER_STYLESHEET` by the Resolve stage.

Only the things that stage invents: the three states a value button can be in,
the small section headings, the operator badge, the machine/manual/effective
strip, and the queue's selected row.

The last of those is a *narrowing*. The application's selection fill is the
accent at full strength, which on a queue whose rows are already tinted by
state made the selected row a solid dark red block - the one colour that means
the brand, the selection, and a warning all at once. Here it is the accent tint
with accent rules above and below, so the selection reads as a selection and
red is left to mean something."""

def attendance_stage_stylesheet(scale: UiScale = UiScale.IDENTITY) -> str:
    """The Attendance stage's own rules at ``scale`` - see the constant below."""
    px = scale.px
    st = scale.stroke
    return f"""
QLabel#attendanceSectionHeading {{
    color: {Color.TEXT_TERTIARY};
    font-weight: {FontWeight.SEMIBOLD};
}}

QPushButton#attendanceCountChip {{
    text-align: left;
    padding: {px(2)}px {px(Spacing.SM)}px;
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-radius: {px(Radius.SM)}px;
    background: {Color.SURFACE};
}}

QPushButton#attendanceCountChip:checked {{
    background: {Color.PRIMARY_SOFT};
    border: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
}}

QTableWidget#reconciliationTable::item:selected,
QTableWidget#setAttendanceTable::item:selected {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.PRIMARY_SOFT};
    border-top: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
}}
"""


ATTENDANCE_STAGE_STYLESHEET: Final = attendance_stage_stylesheet()
"""Set on the Attendance stage.

The section headings that replace a group box around every block, the count
chips that filter the table, and the same narrowed selection the Resolve queue
uses: the accent *tint* with accent rules, so a selected row reads as a
selection and a wide table never turns into a solid red band."""

ANSWER_KEY_STATE_PROPERTY: Final = "keyState"
"""Dynamic property on an Answer Key set tile / status label naming its state:
``verified``, ``draft``, ``missing``, ``stale``, ``unsaved`` or ``invalid``."""

def answer_key_stage_stylesheet(scale: UiScale = UiScale.IDENTITY) -> str:
    """The Answer Key stage's own rules at ``scale`` - see the constant below."""
    px = scale.px
    st = scale.stroke
    return f"""
QLabel#answerKeySectionHeading {{
    color: {Color.TEXT_TERTIARY};
    font-weight: {FontWeight.SEMIBOLD};
}}

QFrame#answerKeySetsBar, QFrame#answerKeySetBox, QFrame#answerKeyActionBar {{
    background: {Color.SURFACE};
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-radius: {px(Radius.MD)}px;
}}

QToolButton#answerKeySetTile {{
    text-align: left;
    padding: {px(2)}px {px(Spacing.SM)}px;
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER};
    border-radius: {px(Radius.SM)}px;
    background: {Color.SURFACE};
    color: {Color.TEXT_PRIMARY};
}}

QToolButton#answerKeySetTile:hover {{
    background: {Color.SURFACE_HOVER};
}}

QToolButton#answerKeySetTile:checked {{
    background: {Color.PRIMARY_SOFT};
    border: {st(Stroke.FOCUS_RING)}px solid {Color.PRIMARY};
}}

QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="verified"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="verified"] {{
    color: {Color.STATUS_READY};
}}

QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="draft"],
QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="unsaved"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="draft"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="unsaved"] {{
    color: {Color.STATUS_BUSY};
}}

QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="missing"],
QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="stale"],
QToolButton#answerKeySetTile[{ANSWER_KEY_STATE_PROPERTY}="invalid"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="missing"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="stale"],
QLabel#answerKeyStatusLabel[{ANSWER_KEY_STATE_PROPERTY}="invalid"] {{
    color: {Color.STATUS_ERROR};
}}

QLabel#answerKeyStatusLabel {{
    font-weight: {FontWeight.SEMIBOLD};
    padding: {px(Spacing.XXS)}px {px(Spacing.SM)}px;
    border: {st(Stroke.HAIRLINE)}px solid {Color.BORDER_STRONG};
    border-radius: {px(Radius.SM)}px;
    background: {Color.SURFACE_MUTED};
}}

QPlainTextEdit#answerKeyTextEdit {{
    font-family: "Consolas", "Cascadia Mono", monospace;
}}

QTableWidget#answerKeyTable::item:selected,
QTableWidget#solutionSheetTable::item:selected {{
    color: {Color.TEXT_PRIMARY};
    background: {Color.PRIMARY_SOFT};
    border-top: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
    border-bottom: {st(Stroke.HAIRLINE)}px solid {Color.PRIMARY};
}}
"""


ANSWER_KEY_STAGE_STYLESHEET: Final = answer_key_stage_stylesheet()
"""Set on the Answer Key stage and its solution-sheet review dialog.

Set tiles and the status label take their colour from the key's state - and
always carry the state in words and a glyph as well, so colour is never the
only carrier. The selected-row narrowing is the one the Attendance and
Resolve stages already use."""


SCALED_STYLESHEETS: Final[dict[str, Callable[[UiScale], str]]] = {
    "template_designer": template_designer_stylesheet,
    "resolve_stage": resolve_stage_stylesheet,
    "attendance_stage": attendance_stage_stylesheet,
    "answer_key_stage": answer_key_stage_stylesheet,
}
"""The page-scoped sheets, by name, for a widget that sets one of them.

A widget that styles itself with one of these declares it through
:func:`omr_scanner.gui.ui_scale.set_scaled_stylesheet` instead of calling
``setStyleSheet`` with the constant, so that an interface zoom change can
recompose the sheet at the new scale. Keyed by name rather than storing the
function on the widget, because a Qt dynamic property survives the Python
wrapper being recreated and a function reference would not."""


__all__ = [
    "ANSWER_KEY_STAGE_STYLESHEET",
    "ANSWER_KEY_STATE_PROPERTY",
    "ATTENDANCE_STAGE_STYLESHEET",
    "CANDIDATE_CHOSEN",
    "CANDIDATE_MACHINE",
    "CANDIDATE_STATE_PROPERTY",
    "RESOLVE_STAGE_STYLESHEET",
    "SCALED_STYLESHEETS",
    "TEMPLATE_DESIGNER_STYLESHEET",
    "VARIANT_DESTRUCTIVE",
    "VARIANT_PRIMARY",
    "VARIANT_PROPERTY",
    "answer_key_stage_stylesheet",
    "application_stylesheet",
    "attendance_stage_stylesheet",
    "resolve_stage_stylesheet",
    "template_designer_stylesheet",
]
