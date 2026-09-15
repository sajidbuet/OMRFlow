"""Loading the bundled toolbar icons.

Purpose:
    Resolve a Lucide SVG icon by name into a `QIcon`, reliably regardless of
    how OMRFlow is run - from a source checkout, via ``python -m``, or from an
    installed package - by reading the file through :mod:`importlib.resources`
    rather than a path relative to the current working directory or the
    source file's location on disk.

Responsibilities:
    * One function, :func:`load_icon`, returning a cached `QIcon` for a
      bundled Lucide icon name (e.g. ``"save"``, ``"folder-open"``).

What does NOT belong here:
    * Any action-to-icon mapping. That lives beside each toolbar's own
      construction code (`template_designer/page.py`,
      `template_designer/region_list.py`), so the mapping is visible next to
      the action it names rather than collected in a lookup table nobody
      updates together with the action itself.

Why `importlib.resources` rather than a `.qrc` file:
    Qt's resource system needs a build step (`pyside6-rcc`) that this project
    does not currently have; `importlib.resources` needs none; it is the
    standard-library mechanism for "a data file that ships inside a Python
    package," works identically for an editable install, a built wheel and a
    frozen executable, and is what Qt's own documentation recommends when a
    `.qrc` pipeline is not already in place.

Why plain files rather than pre-rendered PNGs:
    Every icon is an unmodified Lucide SVG using ``stroke="currentColor"``.
    Qt's SVG icon engine resolves that to the widget's text colour and
    rescales the vector cleanly at any DPI, so one file serves every display
    scale a PNG would need several fixed sizes for.

Licensing:
    Icons are unmodified files from the Lucide icon set
    (https://lucide.dev), ISC licensed (a handful are additionally
    MIT-licensed, inherited from the Feather project Lucide forked from). The
    exact upstream licence text is bundled at
    ``omr_scanner/gui/resources/icons/lucide/LICENSE``, next to the assets, as
    both licences require. See that directory's ``README.md`` for the list of
    icons in use and where each came from.
"""

from __future__ import annotations

from contextlib import ExitStack
from functools import cache
from importlib import resources

from PySide6.QtGui import QIcon

_ICON_ANCHOR_PACKAGE = "omr_scanner.gui"
_ICON_SUBPATH = ("resources", "icons", "lucide")
"""The bundled icons live at ``<omr_scanner.gui package dir>/resources/icons/lucide/``.

Anchored on the real Python package `omr_scanner.gui` rather than on this
module's `__file__`, so resolution keeps working when the package is
imported from inside a zipped wheel or a frozen executable, where there is no
ordinary filesystem path to compute from.
"""

_extracted_files = ExitStack()
"""Keeps any temporary extraction alive for the process lifetime.

Qt's SVG icon engine does not read a `.svg` file once at construction time; it
re-reads the path lazily every time an icon is rendered, at whatever size is
requested. For a normal source checkout or a `pip`-installed package this path
is a permanent file on disk and none of this matters, but
`importlib.resources.as_file()` only guarantees the path stays valid *inside
its own `with` block* - it may extract to a deleted-on-exit temporary file
when the package is read from a zip archive. Never closing this stack (it
lives until the process exits, the same lifetime as the `QIcon` cache below)
is the documented way to keep such a path valid for as long as a library -
here, every icon this session ever loads - needs it.
"""


@cache
def load_icon(name: str) -> QIcon:
    """Return the bundled Lucide icon named ``name``.

    Args:
        name: Icon file name without its ``.svg`` suffix, e.g. ``"save"``,
            ``"folder-open"``.

    Returns:
        A `QIcon` backed by the bundled SVG. Cached, so repeated calls for the
        same name (every toolbar rebuild in tests, for instance) return the
        same instance rather than re-reading the file.

    Raises:
        FileNotFoundError: No bundled icon has that name - a programming
            error (a typo in an action's icon name), not a condition normal
            use can trigger, so it is left to raise rather than silently
            returning a blank icon a user would never understand.
    """
    resource = resources.files(_ICON_ANCHOR_PACKAGE).joinpath(*_ICON_SUBPATH, f"{name}.svg")
    if not resource.is_file():
        raise FileNotFoundError(f"No bundled icon named '{name}.svg'")
    path = _extracted_files.enter_context(resources.as_file(resource))
    icon = QIcon(str(path))
    if icon.isNull():
        raise FileNotFoundError(f"Bundled icon '{name}.svg' could not be loaded as a QIcon")
    return icon


__all__ = ["load_icon"]
