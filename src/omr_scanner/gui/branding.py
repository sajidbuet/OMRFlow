"""Loading the OMR Flow logo and application icon.

Purpose:
    Resolve the bundled branding assets
    (``gui/resources/branding/{logo,icon_mark}.svg``, ``icon.ico``) into
    real filesystem paths and Qt types, reliably regardless of how OMRFlow is
    run - from a source checkout, via ``python -m``, or from an installed
    package - the same `importlib.resources` approach
    :mod:`omr_scanner.gui.icons` already uses for the toolbar icons, and for
    the same reason: a path relative to the current working directory or to
    this file's location on disk breaks under a build tool or a frozen
    executable.

Responsibilities:
    * :func:`logo_svg_path` - the full wordmark, for `QSvgWidget` display.
    * :func:`application_icon` - the multi-resolution window/taskbar icon.

What does NOT belong here:
    * Any layout or widget construction; `main_window.py` decides where the
      logo appears and how the footer looks.
"""

from __future__ import annotations

from contextlib import ExitStack
from functools import cache
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QIcon

_BRANDING_ANCHOR_PACKAGE = "omr_scanner.gui"
_BRANDING_SUBPATH = ("resources", "branding")

LOGO_CONTENT_BOX = (150.0, 324.0, 948.0, 613.0)
"""Where the wordmark's ink actually is inside ``logo.svg``, as
``(x, y, width, height)`` in the file's own 1254x1254 user units.

Measured once from the artwork's path bounds (`QSvgRenderer.boundsOnElement`),
and load-bearing rather than informational. The file's `viewBox` is the full
square canvas, and `QSvgWidget` renders *that* into whatever rectangle the
widget occupies, stretching it to fit - so a widget sized to the ink's own
1.55:1 ratio squashed the square canvas into it and distorted the wordmark by
a factor of 1.55, while surrounding it with transparent padding that wasted
about a third of the width it was given. :func:`logo_view_box` is what a
caller uses to render the ink and nothing else.
"""

LOGO_ASPECT_RATIO = LOGO_CONTENT_BOX[2] / LOGO_CONTENT_BOX[3]
"""Width divided by height of ``logo.svg``'s visible content (not its square
1254x1254 canvas). Used to size the chrome row's logo widget without
distorting it - `QSvgWidget` does not infer a "natural" size for artwork that
does not fill its own `viewBox` the way an `<img>` would."""


def logo_view_box() -> QRectF:
    """The rectangle a renderer should treat as the whole logo.

    Returns:
        :data:`LOGO_CONTENT_BOX` as a `QRectF`, to hand to
        `QSvgRenderer.setViewBox`. After that the renderer maps the ink - not
        the empty canvas around it - onto the widget's rectangle, so a widget
        at :data:`LOGO_ASPECT_RATIO` shows the wordmark at its true
        proportions and at the full size it was given.
    """
    return QRectF(*LOGO_CONTENT_BOX)

_extracted_files = ExitStack()
"""Keeps any temporary extraction alive for the process lifetime - see the
identical pattern, and the full explanation, in `gui/icons.py`."""


def _bundled_path(filename: str) -> Path:
    """Return a real, persistent filesystem path to a bundled branding file."""
    resource = resources.files(_BRANDING_ANCHOR_PACKAGE).joinpath(*_BRANDING_SUBPATH, filename)
    if not resource.is_file():
        raise FileNotFoundError(f"No bundled branding asset named '{filename}'")
    return _extracted_files.enter_context(resources.as_file(resource))


def logo_svg_path() -> Path:
    """Path to the full "OMR Flow" wordmark SVG, for `QSvgWidget`."""
    return _bundled_path("logo.svg")


@cache
def application_icon() -> QIcon:
    """The application/window icon: a multi-resolution `QIcon` from `icon.ico`.

    Returns:
        A `QIcon` exposing every size baked into `icon.ico` (16 through 256
        px), so Qt and the platform (title bar, taskbar, alt-tab switcher)
        each pick the resolution they need rather than scaling one bitmap.

    Raises:
        FileNotFoundError: The bundled icon is missing - a packaging error,
            not a condition normal use can trigger.
    """
    path = _bundled_path("icon.ico")
    icon = QIcon(str(path))
    if icon.isNull():
        raise FileNotFoundError(f"Bundled application icon '{path}' could not be loaded")
    return icon


__all__ = [
    "LOGO_ASPECT_RATIO",
    "LOGO_CONTENT_BOX",
    "application_icon",
    "logo_svg_path",
    "logo_view_box",
]
