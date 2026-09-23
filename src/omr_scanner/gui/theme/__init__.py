"""The application's visual design system.

Purpose:
    Hold every visual constant in one importable place, and compose them into
    the stylesheets the application applies.

Layout:
    * :mod:`.tokens` - the data: colours, spacing, radii, type sizes and the
      metrics of the shell components. No Qt import, so the scale is testable
      without a display.
    * :mod:`.stylesheet` - the Qt stylesheets built from those tokens.

Why this is a package and not the single module it used to be:
    The module that lived here did one job - give the editor toolbars legible
    colours for every interaction state - and named its own private palette to
    do it. Once the whole shell needed the same decisions, that palette had to
    become the application's, which is a different responsibility from
    composing one page's stylesheet. Splitting them keeps the design tokens
    free of Qt (and therefore cheap to test) while the public import path,
    ``from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET``, is
    exactly what it was.
"""

from __future__ import annotations

from omr_scanner.gui.theme.stylesheet import (
    TEMPLATE_DESIGNER_STYLESHEET,
    VARIANT_DESTRUCTIVE,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    application_stylesheet,
)
from omr_scanner.gui.theme.tokens import (
    Card,
    Chrome,
    Color,
    Dashboard,
    Density,
    DensityLevel,
    FontSize,
    FontWeight,
    Footer,
    IconSize,
    Navigator,
    Radius,
    Spacing,
    Stroke,
)

__all__ = [
    "TEMPLATE_DESIGNER_STYLESHEET",
    "VARIANT_DESTRUCTIVE",
    "VARIANT_PRIMARY",
    "VARIANT_PROPERTY",
    "Card",
    "Chrome",
    "Color",
    "Dashboard",
    "Density",
    "DensityLevel",
    "FontSize",
    "FontWeight",
    "Footer",
    "IconSize",
    "Navigator",
    "Radius",
    "Spacing",
    "Stroke",
    "application_stylesheet",
]
