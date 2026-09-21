"""Button roles, as a function rather than a subclass per role.

Purpose:
    Make "this is the primary action" one call, so the accent lands on exactly
    one button per view and every other button keeps the neutral treatment.

What does NOT belong here:
    * Any colour. The appearance is entirely in the global stylesheet; this
      module only sets the property the stylesheet selects on.

Why the property has to be repolished:
    Qt resolves stylesheet selectors when a widget is polished. Setting a
    dynamic property afterwards does not re-run that, so the rule matches but
    is never applied - the classic symptom being a primary button that is
    styled correctly at start-up and neutral after any state change. Calling
    ``unpolish``/``polish`` is the documented fix and is the only reason this
    function exists rather than a bare ``setProperty`` at each use site.
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from omr_scanner.gui.theme import VARIANT_DESTRUCTIVE, VARIANT_PRIMARY, VARIANT_PROPERTY


def set_button_variant(button: QPushButton, variant: str | None) -> None:
    """Give ``button`` a role, and make Qt notice.

    Args:
        button: The button to restyle.
        variant: :data:`~omr_scanner.gui.theme.VARIANT_PRIMARY`,
            :data:`~omr_scanner.gui.theme.VARIANT_DESTRUCTIVE`, or ``None``
            for the neutral default.
    """
    button.setProperty(VARIANT_PROPERTY, variant)
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    button.update()


def primary_button(text: str, object_name: str = "") -> QPushButton:
    """A new accent-coloured button - the one principal action of a view."""
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    set_button_variant(button, VARIANT_PRIMARY)
    return button


def secondary_button(text: str, object_name: str = "") -> QPushButton:
    """A new neutral button, for everything that is not the principal action."""
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    set_button_variant(button, None)
    return button


def destructive_button(text: str, object_name: str = "") -> QPushButton:
    """A new outlined button for an action that destroys something.

    Distinguished from :func:`primary_button` on purpose: "proceed" and
    "delete permanently" must not be the same colour, or an operator learns to
    click the accent without reading it.
    """
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    set_button_variant(button, VARIANT_DESTRUCTIVE)
    return button


__all__ = [
    "destructive_button",
    "primary_button",
    "secondary_button",
    "set_button_variant",
]
