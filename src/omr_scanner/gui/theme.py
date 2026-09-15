"""Centralised colour constants and the Template Designer's stylesheet.

Purpose:
    Give the toolbar and its controls explicit, legible colours for every
    interaction state (normal, hover, pressed, checked, disabled) instead of
    leaving that entirely to whichever native Qt style happens to be active -
    which is what produced the low-contrast disabled controls this module
    exists to fix.

Responsibilities:
    * Name every colour once (:data:`Colors`) so nothing below duplicates a
      hex value, and a future palette change happens in one place.
    * Compose those colours into one Qt stylesheet,
      :data:`TEMPLATE_DESIGNER_STYLESHEET`, scoped to `QToolBar`/`QToolButton`/
      `QPushButton` - the controls this pass touches - so applying it to the
      Template Designer page cannot alter any other page's appearance.

What does NOT belong here:
    * Any widget construction. This module only produces a stylesheet string;
      `template_designer/page.py` is the one place that applies it.
    * A full application theme/dark mode. This is deliberately scoped to the
      light-theme readability problem this task was written to fix, not a
      general theming system.
"""

from __future__ import annotations

from typing import Final


class Colors:
    """Named colours for the Template Designer's light theme.

    Grouped as a plain namespace (not an enum) so a colour reads as
    ``Colors.TEXT_DISABLED`` at every use site - self-explanatory without a
    comment - rather than an unexplained hex literal.
    """

    TEXT_NORMAL: Final = "#1A1A1A"
    """Dark charcoal - normal, enabled control text and icon colour (SVG icons
    use ``stroke="currentColor"``, which resolves to this)."""

    TEXT_DISABLED: Final = "#8A8A8A"
    """Medium grey - legible on the toolbar background, and unambiguously
    distinct from :data:`TEXT_NORMAL` (this is the low-contrast readability
    problem this module exists to fix)."""

    TOOLBAR_BACKGROUND: Final = "#F5F5F5"
    """Very light neutral grey toolbar background."""

    BUTTON_BACKGROUND: Final = "#FAFAFA"
    """Slightly lighter than the toolbar itself, so a button reads as a
    distinct, clickable surface at rest."""

    BUTTON_BORDER: Final = "#D0D0D0"

    HOVER_BACKGROUND: Final = "#E4E4E4"
    """Slightly darker than the toolbar background - a visible, subtle hover
    state that never competes with the checked/pressed colours below."""

    PRESSED_BACKGROUND: Final = "#D6D6D6"

    CHECKED_BACKGROUND: Final = "#CFE3FB"
    """A clearly distinguishable light blue "on" state for toggle actions
    (Grid, Edit Bubbles) - never communicated by colour alone, since the
    action's own tooltip and (for Grid) the visible grid overlay confirm the
    state too."""

    CHECKED_BORDER: Final = "#5B9BD5"

    DISABLED_BACKGROUND: Final = "#F5F5F5"
    """Same as the toolbar background: a disabled control should not draw a
    button-shaped surface at all, only its (grey) text/icon."""


TEMPLATE_DESIGNER_STYLESHEET: Final = f"""
QToolBar {{
    background: {Colors.TOOLBAR_BACKGROUND};
    border: none;
    spacing: 2px;
    padding: 3px;
}}

QToolButton {{
    color: {Colors.TEXT_NORMAL};
    background: {Colors.BUTTON_BACKGROUND};
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 4px 8px;
}}

QToolButton:hover {{
    background: {Colors.HOVER_BACKGROUND};
    border: 1px solid {Colors.BUTTON_BORDER};
}}

QToolButton:pressed {{
    background: {Colors.PRESSED_BACKGROUND};
}}

QToolButton:checked {{
    background: {Colors.CHECKED_BACKGROUND};
    border: 1px solid {Colors.CHECKED_BORDER};
}}

QToolButton:disabled {{
    color: {Colors.TEXT_DISABLED};
    background: {Colors.DISABLED_BACKGROUND};
    border: 1px solid transparent;
}}

QToolBar::separator {{
    background: {Colors.BUTTON_BORDER};
    width: 1px;
    margin: 4px 4px;
}}

QPushButton {{
    color: {Colors.TEXT_NORMAL};
}}

QPushButton:disabled {{
    color: {Colors.TEXT_DISABLED};
}}

QLabel:disabled {{
    color: {Colors.TEXT_DISABLED};
}}
"""
"""Applied to the Template Designer page only (``self.setStyleSheet(...)`` in
``TemplateDesignerPage.__init__``), so no other page's controls change
appearance. Qt stylesheets cascade to child widgets, so this one declaration
covers the toolbar and every button/label inside the page."""


__all__ = ["TEMPLATE_DESIGNER_STYLESHEET", "Colors"]
